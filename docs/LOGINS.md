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
3. **Ask.** "What is on my timetable tomorrow?" Delta calls `sign_in`, opens the site in the browser you use with
   Delta (its private browser, or your own default browser), signs in and reads the page.

Saving again with the same name asks whether to **Replace** it: the login is updated in place, never copied. A login
saved without a password says so on its card ("NO password saved") and in the save message.

Logins can only be added or changed on the PC. A password never goes through Telegram or any chat.

## "Log in to my ERP": no AI at all

A request that only asks to sign in ("Login my college ERP portal", "sign in to euerp") runs `sign_in` directly: no
planner, no agent loop, **0 AI calls and 0 tokens**. Delta finds the login from your words (its name or its site:
"Example University ERP" finds a login named "euerp" for `https://newerp.example-university.edu`), opens the page,
fills it, handles a simple captcha (below), submits once and checks the sign-in worked (the form is gone). Two logins that both
fit are never guessed between: Delta names them and asks. "try again" or "done, try again now" in the same chat
re-runs the same sign-in, again without AI. A request that asks for more ("log in and check my timetable") goes to the
agent as before.

When something stops it, the message says exactly what: which login, which field is missing ("'euerp' has no
password saved (only a user name)"), which address did not match the saved site, or that the form stayed hidden. It
never says a password is missing when one is saved.

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
- **A simple text captcha** (a picture of a few characters, as on many college ERPs). Delta reads it **by itself,
  with no approval**, on every saved login that has a password: the switch **"This site has a simple text captcha,
  let Delta read it"** is **on by default** (Settings > Logins, on the add form and on the login's card; or
  `delta logins captcha NAME --on / --off`). Turn it off for a login and Delta sends you the picture every time
  instead. It does what `delta captcha allow <host>` does, for that login's exact origin only.
  How Delta reads it, in order:
  1. **The captcha model on this PC.** [ddddocr](https://github.com/sml2h3/ddddocr) (MIT licence), a small offline
     model made for simple text captchas, run on the CPU with onnxruntime (MIT). The installer adds it (the
     `captcha` extra, part of `--extra all`; about 90 MB on disk, most of it the model). It reads three cleaned-up
     versions of the picture (grayscale and contrast, Otsu threshold, median denoise), about 25 ms each. The captcha
     box itself says what fits (its `maxlength`, `minlength`, `pattern` such as `[0-9]{5}`, `inputmode`): only
     characters the box takes can be chosen, inside the model's own choice, and a box that says capitals only or
     small letters only decides the case. When the three readings agree and no letter's case was a close call, that
     answer is typed and nothing else runs: **zero AI calls**. Missing or broken (an unusual PC, a broken install):
     Delta goes on with Windows OCR and nothing else changes.
  2. **Windows OCR on this PC**, only when the model was not sure. The picture is cleaned up five ways (grayscale and
     contrast, Otsu threshold, median denoise, thin lines and dots removed, darkest ink only), enlarged, and read in
     **one** OCR call; the usual mix-ups (0/O, 1/l/I, 5/S, 2/Z) are fixed when the box takes only digits or only
     letters, and the passes vote per character. When OCR lands on what the model read (its vote, one of its
     readings, or the same characters in another case, which OCR then settles), that is typed.
  3. **One small picture to your AI**, only when the engines disagree, a reading does not fit the box or nothing was
     read, and only when a configured model takes pictures and you let pictures go to the AI (`attachments.vision`
     is not `off`): a tight crop of just the captcha (at most 256 x 72 pixels, low detail), at most one call per try,
     instead of typing a likely wrong guess. Counted in the audit (`via: vision`). With no such model, the captcha
     model's best reading is typed (a wrong one costs one of the 3 tries).
  What is logged: for each engine, how many passes read something that fits the box, their lengths and whether they
  agree (`captcha.read`). Never the picture, the readings or the answer; set `DELTA_CAPTCHA_DEBUG=1` yourself to add
  the readings to Delta's log for a bug report, and unset it afterwards.
  3. At most **3 tries** per site in 15 minutes, asking the site for a new picture when one cannot be read and filling
     the form again when the site says only the captcha was wrong ("The verification code is incorrect"). The
     sign-in is checked after every submit. A rejected *password* is never sent again.
  When the switch is off, or the 3 tries are used up, Delta keeps the filled form open and **sends you the captcha
  picture** in the chat you asked from (the dashboard shows it with the answer, and again if you reload the page;
  Telegram gets it as a photo). Reply with just the characters and Delta types them into that same form (after
  checking the tab and the exact site again), submits once and checks the result. A wrong reply brings the new
  picture (3 replies at most); "cancel" stops; it expires after 10 minutes. Your reply is never written to a log or
  the audit. When you asked from your phone, Delta never tells you to "complete it in the open browser": if no
  picture could be taken at all, it says so and submits nothing.
  A captcha box is never taken for a one-time-code box, even when it says "Enter verification Code".
  Logins saved before this change: a one-time migration turns the switch on for each login with a password, unless
  you had turned it off. A switch turned off in Settings > Logins is read from the audit log. A switch turned off
  with `delta logins captcha NAME --off` (not audited then) is read from the login file itself: it says off, it was
  written after the switch first shipped, and that write was not a save Delta stamps (a new login, a sign-in, an
  own-browser consent). A permission change or a Replace from that time also reads as off, the safe side: you get
  the picture and can turn reading back on. If you already ran the version that brought the new default before
  this fix, that version had already rewritten those files, so check the switch on each login once.
- **Wrong password.** Delta stops at once and says so. It never retries a rejected password.

## Which browser: Delta's private browser or your own

Logins work in both browser choices (Settings > Browser, `/browser` on Telegram, or "use my default browser"):

| Browser choice | Where Delta types the login |
|---|---|
| Delta's private browser | Its own separate profile, in the background. |
| My default browser, Chrome or Edge | **Your everyday browser**, in the one window Delta opens there, with your real sessions and extensions. |

`sign_in` never changes your browser choice. It signs in where the task already is. (Only when your own browser
cannot be driven on this PC does that one task use the private browser instead, and Delta says so.)

Every rule above applies the same way in your own browser: the exact origin, no lookalike or punycode host, no
http downgrade, no other port, no form inside a frame from another site, the password never shown to the AI,
written to a log or kept in memory, a wrong password never retried, the same permission and PIN rules, and no
saving without DPAPI. Your own browser is your real browser, so Delta adds these:

- **A one-time consent per site.** The first time Delta would type a saved login into your own browser, it asks
  you first, naming the browser and the site: *"First time in your own browser: let Delta type your saved login
  for College ERP (https://erp.example.edu) into Google Chrome, your everyday browser with your real tabs,
  sessions and extensions?"* It asks this **even when the login is "Always allow"**. There is no "Allow for this
  task" button: Approve once and Delta remembers it for that site in that browser (Chrome and Edge are asked
  separately). Deny, or no answer, and nothing is typed. From a phone, approving needs your PIN, and nothing is
  asked while remote control is locked. To withdraw it, set the login to "Ask every time" (that clears the
  consent too) or remove the login. Replacing a login's password also clears it.
- **Bound to one tab.** The sign-in is tied to the tab that is open in Delta's window when it starts. Before and
  after every field, and before pressing Enter, Delta checks that this tab is still the one in front of it and
  that the page and the frame holding the form are still on the saved site. If you switch tabs or windows, close
  Delta's window, use the mouse or keyboard, or the page goes somewhere else, Delta stops at once, clears what it
  typed and does not submit. Your other tabs and windows are never touched.
- **Typed into the box, not the keyboard.** Delta puts the value straight into that box through Windows UI
  Automation, so it cannot land in another tab, and nothing goes through the clipboard. If a page ignores that,
  Delta types it into the box a few characters at a time, checking before each burst that the box still has the
  keyboard focus.
- **Checked after filling.** If the box does not hold exactly what Delta filled (a password manager extension or
  the page changed or cleared it), Delta says so plainly, does not submit and does not try again.
- **One normal submit.** Delta presses Enter in the password box once, as you would. A simple text captcha follows
  the same rule as in the private browser: read by Delta when the login's captcha switch is on (the default), else
  the picture is sent to you and your reply is typed into the same tab (the tab binding and origin checks run again
  first). Delta takes the picture from the screen, only that rectangle, with its window in front, per-monitor DPI
  aware (display scaling and a second monitor), including a captcha drawn on a `<canvas>` whose id says captcha. With
  no picture element at all, you get a band of the page around the captcha box with every password box painted over
  (that band is never read by OCR or sent to the AI). Only if not even that can be taken, and only while you are at
  the PC, do you type the characters and press Sign in yourself. A "code" box shown on the same form as the password
  box is treated as the captcha, never submitted empty.
- **Pictures stay masked.** Your browser already shows dots in a password box. After Delta fills one, it also paints
  that area over in every screenshot Delta takes (one window or the whole screen), in case the page or a "show
  password" button turns the box into plain text.
- **Watchers stay in the private browser.** Scheduled watcher checks run in the background, so they always use
  Delta's private browser, never your own.

The page's address is read from the page itself (UI Automation's document value), never from the address bar,
which hides "http://" and "https://". If Delta cannot read the full address of the page or of the frame holding
the form, it refuses.

## Every use is audited

The audit log gets the login's name, its site, the task, which browser (`private` or `own:chrome` / `own:edge`), how
it was allowed and the outcome. The one-time own-browser consent is audited too (`login.own_browser_consent`). Never
the user name, the password, a one-time code or a captcha answer.

## Commands

| Where | Command |
|---|---|
| Terminal | `delta logins add NAME --site URL [--login-url URL] [--username U] [--always] [--allow-http] [--replace]` |
| Terminal | `delta logins list`, `delta logins remove NAME`, `delta logins test NAME`, `delta logins allow NAME --always / --ask` |
| Terminal | `delta logins captcha NAME --on / --off`, `delta logins add ... --simple-captcha` |
| Dashboard | Settings > Logins and watchers: add, replace, test, remove, change permission, the simple-captcha switch |
| Telegram | `/logins`: names, sites and permissions only (read-only) |
| The AI | `list_logins`, `sign_in(site)` |

## Better browser actions

The same batch made Delta's private browser steadier on real portals: actions retry what is worth retrying and check
afterwards that the page really changed, elements inside frames and new tabs can be used, dialogs can be accepted
or dismissed, long lists scroll, and there are new tools for dropdowns (`browser_select`), checkboxes
(`browser_check`), file upload (`browser_upload`, which asks first because the file leaves the PC), hover
(`browser_hover`), waits (`browser_wait`), tabs (`browser_tabs`) and dialogs (`browser_dialog`).

These tools also work in your own browser, with small differences: elements are found by their label or visible
text (there is no numbered page listing there), `browser_tabs` only lists, switches or closes the tabs of Delta's
own window, `browser_dialog` answers the dialog showing now (in the private browser it decides the next one), and
`browser_upload` fills the browser's own Open dialog with the file's path. The `solve_captcha` tool stays
private-browser only (a saved login's own captcha handling above works in both).

See also [WATCHERS.md](WATCHERS.md) for checking a portal on a schedule, and [NOT_VERIFIED.md](NOT_VERIFIED.md) for
what has not been tried on real hardware yet.
