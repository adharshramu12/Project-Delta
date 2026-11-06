# Saved site logins

Delta can sign in to your own accounts on sites such as a college ERP or LMS, so it can read your timetable, check
exams or keep an eye on notices for you. You save the login once, on the PC. After that you just ask: "check my ERP
timetable for Tuesday". Delta types the user name and password itself, only on the exact site you saved, and the AI
never sees them.

## Quickstart

1. **Save the login on the PC.** In the dashboard open **Settings > Logins and watchers** and fill in a name
   ("College ERP"), the site's address, your user name and password. Or in a terminal:

   ```
   delta logins add "College ERP" --site https://erp.example.edu
   ```

   The password is asked for without being shown, so it is never in the shell history or the process list.
2. **Choose the permission.** "Ask every time" (the default) or "Always allow for this site".
3. **Ask.** "What is on my timetable tomorrow?" Delta calls `sign_in`, opens the site in its private browser, signs
   in and reads the page.

Logins can only be added or changed on the PC. A password never goes through Telegram or any chat.

## How a login is kept

- One file per login in Delta's data folder (`logins/<id>.bin`), sealed with **Windows DPAPI**. A sealed file opens
  only for the same Windows account on the same PC. If DPAPI does not work, saving is refused: Delta never writes a
  password in plain text.
- Delta's data folder is a protected path: no tool, shell command or file read can open it.
- The password is read only at the moment it is typed into a page that passed the origin checks below. It is never
  in a log line, the audit log, a chat, a screenshot (password boxes are masked first) or the model's context.

## Where Delta will type it

A login is bound to one **exact origin**: scheme, host and port, as browsers define it. Delta types the password
only when the page and the frame holding the form have exactly that origin. It refuses, with a plain reason, and
types nothing on:

- another host, including lookalikes (`erp.example.edu.evil.test`, `erp-example.edu`, punycode, another subdomain,
  an IP address instead of the name);
- an http page when the login was saved for https (a downgrade);
- another port;
- a sign-in form inside a frame from another origin.

A login for a plain http site can be saved only on purpose (tick "The site only has plain http"), and is then used
only on that http origin.

## Permission

| Setting | What happens |
|---|---|
| Ask every time (default) | Each use asks first on every approval channel (dashboard, Telegram, terminal). "Allow for this task" covers the rest of that one task only and is never saved. |
| Always allow for this site | Used without asking, on that exact site only. |

From a phone: approving a sign-in needs your PIN, a request from Telegram can sign in only after `/unlock`, and
while remote control is locked nothing remote signs in. Change the setting in Settings or with
`delta logins allow "College ERP" --always` (or `--ask`).

## When a site needs you

- **One-time code (OTP) or second step.** Delta pauses on an approval: type the code into the page on the PC, then
  Approve. A request from Telegram also gets a picture of the waiting page (password boxes masked). Deny or no
  answer stops the sign-in.
- **reCAPTCHA, hCaptcha, Cloudflare Turnstile.** Never solved by Delta. The same hand-off: you complete it on the
  PC, or the sign-in stops.
- **A simple image captcha** on your own account's form follows the captcha rule: only on sites you allowed with
  `delta captcha allow <host>`, read with Windows OCR first, at most 3 attempts, then a hand-off to you.
- **Wrong password.** Delta stops at once and says so. It never retries a rejected password.

## Every use is audited

The audit log gets the login's name, its site, the task, how it was allowed and the outcome. Never the user name,
the password, a one-time code or a captcha answer.

## Commands

| Where | Command |
|---|---|
| Terminal | `delta logins add NAME --site URL [--login-url URL] [--username U] [--always] [--allow-http] [--replace]` |
| Terminal | `delta logins list`, `delta logins remove NAME`, `delta logins test NAME`, `delta logins allow NAME --always / --ask` |
| Dashboard | Settings > Logins and watchers: add, test, remove, change permission |
| Telegram | `/logins`: names, sites and permissions only (read-only) |
| The AI | `list_logins`, `sign_in(site)` |

## Better browser actions

The same batch made Delta's private browser steadier on real portals: actions retry what is worth retrying and check
afterwards that the page really changed, elements inside frames and new tabs can be used, dialogs can be accepted
or dismissed, long lists scroll, and there are new tools for dropdowns (`browser_select`), checkboxes
(`browser_check`), file upload (`browser_upload`, which asks first because the file leaves the PC), hover
(`browser_hover`), waits (`browser_wait`), tabs (`browser_tabs`) and dialogs (`browser_dialog`).

See also [WATCHERS.md](WATCHERS.md) for checking a portal on a schedule, and [NOT_VERIFIED.md](NOT_VERIFIED.md) for
what has not been tried on real hardware yet.
