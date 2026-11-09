# Changelog

All notable changes to Delta are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and Delta uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version lives in one place,
`pyproject.toml`; `delta --version` reads it from there.

## Unreleased

### Added
- **Live progress, so Delta never looks stuck.** While a task runs, the chat shows what Delta is doing now in plain
  words ("Working in Notion: search", "Opening youtube.com") with a timer and a soft shimmer, and "Still waiting"
  when one step takes long. On Telegram, a task that takes more than a few seconds gets one status message that is
  edited as it goes and ends with Finished, Stopped or Could not finish. The words are worked out on the PC from the
  tool and its arguments, with no AI call.
- **"Waiting for the task already running to finish"** in the chat and on Telegram when a request has to queue
  (Delta runs one heavy task at a time), instead of silence.

### Changed
- **A task never runs for ever.** After `agent.task_timeout_s` (20 minutes by default, 0 turns it off) it is stopped
  and you are told the last thing it was doing.
- **Delta stays small on disk.** Its two browser profiles (the dashboard window and the private browser) start with a
  capped disk cache (32 MB and 100 MB), and a minute after start Delta removes their regenerable leftovers: Edge and
  Chrome components, provenance data, metrics, crash dumps, shader and web caches, and the window's built-in
  extensions. Only profiles no browser is using are touched, and sign-ins, cookies, settings and Local Storage are
  never removed. On a real PC the window's profile had grown to 737 MB for a dashboard page. The installer also
  deletes its 130 MB download cache when it is done.

## 0.3.0 - 2026-11-09

The first version under AGPL-3.0-or-later, and the first that tells you when a newer one is out. In short:

- **The AI you pick answers** (#50): Groq, NVIDIA, Cline and the others are used when you choose them,
  refused keys are named, Cline works on Windows, and prompts are smaller.
- **Pictures, Gmail and Telegram** (#49, #52): pictures are really seen, Gmail is filled field by field
  with Save as draft, Telegram survives slow networks; steadier Stop overlay and CI.
- **First-run tour and update notifications** (#48): a short, skippable tour of the
  dashboard after setup, and a banner when a newer Delta is out, with Update now that installs exactly the checked
  release and opens Delta again. Details below and in [docs/UPDATES.md](docs/UPDATES.md).
- **Fresh-user fixes** (#46, #47): a CI journey that installs, uses, updates and uninstalls Delta as a new Windows
  user would; start with Windows; clearer no-model hints; uninstall and update clean-ups.
- **Captchas read by Delta itself** (#43, #45): an offline captcha model first, Windows OCR second, one small vision
  call only when they disagree; on by default for saved logins, with a picture fallback that works away from the PC.
- **Saved logins** (#40): sign-ins with no AI call, exact error messages, a per-login simple-captcha switch, and the
  captcha answer from the chat.
- **Windows install** (#42): the real one-line install tested on five Windows set-ups, Unicode-safe shortcuts, and
  setup checks with fixes in `delta doctor`.
- **Crash guard and fewer tokens** (#44): an unexpected error is logged and shown and Delta carries on; apps open
  at once; smaller, cache-friendly prompts.
- **Relicensed to AGPL-3.0-or-later** with a trademark policy and a new README (#41). Versions up to and including
  0.2.0 were Apache-2.0.
- **Writing, email and apps** (#39): a human writing style, email with approval for each email, MCP settings in Apps.
- **Telegram** (#37): the full menu, grouped, old chats from the phone, long chats without guessing.
- **In control and stop anytime** (#38): the orange "Delta is in control" ring and border, Esc twice to stop, faster
  app opening, bounded self-heal.
- **Dashboard** (#36): the window is always filled; Telegram chat ids are hidden.
- Also since the 0.2.0 version number (merged before #36): fresh-laptop audit fixes (#28), the dashboard redesign
  (#29), attachments in chat and Telegram (#30), the delta command and Update Delta in the Start menu (#31), site
  logins, safer browser actions and portal watchers (#32), saved logins in your own browser (#34), the installer
  under the Restricted policy (#35), and dependency updates (#26, #27).


### Changed (#50)

- **The AI you pick is the AI that answers.** With a main AI chosen in Apps > AI models (Groq, NVIDIA, Cline, Codex,
  ...), every planning, doing and checking step uses it. Before, any failure of it (for example an expired Groq key)
  quietly handed the task to the next AI on the route, usually Codex, which then used your Codex plan. A new switch,
  "If my main AI fails, let my other AIs answer" (`llm.fallback`, off by default), brings that back when you want it.
  "Automatic" still follows the route as before, and a job your main AI cannot do at all (reading a screenshot with
  a text-only AI) still uses the route.
- **Refused keys are named.** A Groq, NVIDIA or DeepSeek key that is expired, revoked or mistyped now says "Groq key
  expired or invalid. Add a new one in Settings > Keys" instead of a bare HTTP 401, and is not retried on every
  message until you save a new one. NVIDIA "no credits left" (HTTP 402) is reported as used-up credits.
- **Each answer and task says which AI and model ran it**: "via groq (openai/gpt-oss-120b)" under the answer and in
  the Tasks list, counted from this task's own calls.
- **Cline on Windows**: `cline.cmd` from npm is run through node directly, so Cline gets the whole prompt (cmd.exe cut
  it at the first line break and would read `&` or `|` in a web page as commands); the command-line limit now counts
  Windows quoting, so an over-long prompt is shortened and retried instead of Cline failing to start.
- **Fewer tokens per step**: tools are described to Codex, Claude Code, Cline and local models as short signatures
  instead of JSON schemas, API providers get lean schemas, and rules about tools a task is not given are left out.
  `evals/token_bench.py`: 44,766 to 39,729 estimated input tokens over its six tasks (-11%), and 177,019 to 145,443
  characters of CLI prompt (-18%).

### Fixed (#49, #52)

- **Pictures are recognised**: a picture from the dashboard or Telegram is looked at unless only its text is wanted;
  transparent PNG/WebP/GIF no longer reach the model as black squares; pictures are kept under a size every vision
  API takes; a model that says it cannot see images is not taken as an answer (the next one is asked); the user
  hears why a picture was not seen, with models that can see it. iPhone HEIC photos open with `pillow-heif`.
- **Writing an email in Gmail**: the compose window is filled and read back field by field (recipients picked by
  exact address, Subject, body as plain text) before anything is saved or sent; new `save_email_draft` keeps it in
  Gmail Drafts without sending; typing into Gmail's compose fields with browser_fill points to the email tools.
- **Telegram keeps working on slow networks**: timeouts fit slow links (were 5 s), replies are retried, "typing..."
  never stops a request, a long task says "Still working on it", the poll loop is watched and restarted, and the
  dashboard shows a plain line only for a sustained outage (two copies on one bot and a bad token have their own).
- **CI and Delta's on-screen control**: the control overlay gets a new window-class name each time (the old
  name could be reused and Windows refused it, error 1410, so the Stop overlay could fail to start); a browser
  closing its connection mid-request is no longer logged as a crash; the dashboard's first load in the e2e
  tests gets 45 s on a cold runner.

### Added
- **First-run tour.** After setup, a short tour (nine steps, about a minute) points at the real dashboard: what Delta
  is, the chat box (try "open notepad"), Tasks (Stop, or Esc twice), Apps, Schedules, Documents and Memory, Settings >
  Keys (a free Groq key), Settings > Logins (saved sign-ins and simple captchas), and Telegram and start with Windows.
  It shows once per PC (saved in your settings file, not the browser), Esc skips it, the arrow keys move through it,
  Tab stays inside it, screen readers read each step, and it follows light and dark mode, reduced motion, high
  contrast and the window size; a step whose part of the page is hidden (a narrow window) is skipped. Take it again
  from Settings > Updates and help, or type `/tour` in the chat box. No library and nothing fetched.
- **Update notifications.** Delta asks GitHub for a newer version soon after it starts and every 12 hours (with
  jitter): the latest GitHub release (drafts and pre-releases only on the beta channel), or, while there is no
  release, the version in pyproject.toml on main. Only a plain GET with a `Delta/<version>` User-Agent is sent;
  proxies are used; answers are cached with their ETag, and a rate limit or an offline PC backs off quietly. A newer
  version shows a banner with its highlights (cleaned to plain text) and Update now, What's new, Later and Skip this
  version, plus one Telegram message per version when Telegram is connected. Settings > Updates and help turns
  checks off, picks the beta channel, turns off the Telegram message, and checks now. `delta doctor` shows this
  version and the newest one.
- **Update now** runs the safe `delta update` path, never by itself: it asks first, waits for a running task (or stops
  it if you say so), then installs exactly the commit the release tag points to. Before anything changes, the
  installer checks that the downloaded pyproject.toml and uv.lock are byte for byte the files Delta read from that
  commit (SHA-256) and that the version matches; uv.lock then pins every package by its hash. Your data folder is
  never touched. Delta closes, updates in a window that shows each step, opens again, and shows What's new once.
  `delta update` in a terminal is pinned and checked the same way (and falls back to main, as before, when GitHub
  cannot be asked).
- **Start Delta when I sign in to Windows** (Settings > Setup and shutdown, off unless you turn it on; setup offers it
  when you connect Telegram). Without it Delta, and your Telegram bot, stayed off after a restart until you opened
  Delta again. It is one entry for your account only (`HKCU\...\Run`, named `DeltaAgent`, no administrator rights)
  that runs `pythonw.exe -m delta app --background`: the service starts with no window. Turning it off, or
  uninstalling, removes it. A choice made in Task Manager's Startup apps is shown and respected.

### Changed
- **Simple captchas are read far more often by Delta itself.** A small offline model made for text captchas
  ([ddddocr](https://github.com/sml2h3/ddddocr), MIT, run on the CPU with onnxruntime, MIT) is now the first reader,
  added by the installer (`captcha` extra, part of `--extra all`, about 90 MB). It reads three cleaned-up versions of
  the picture, held to the captcha box's own rules (`maxlength`, `pattern`, `inputmode`) inside its own choice, and
  types the answer when all three agree and no letter's case was a close call. Otherwise Windows OCR reads it, and
  when the two still disagree (or nothing fits the box) one small low-detail vision call is made, counted, instead
  of typing a likely wrong guess. Without the model (an unusual PC, a broken install) Windows OCR is the first reader
  as before. Per-engine log lines hold only lengths and agreement, never the picture or the answer
  (`DELTA_CAPTCHA_DEBUG=1` adds the readings, for a bug report). New benchmark of 120 generated ERP-style captchas in
  three styles (`python -m delta.evals.captcha_bench`, run in CI on Windows; a fast subset in the tests); results
  in [docs/LOGINS.md](docs/LOGINS.md). reCAPTCHA, hCaptcha, Turnstile and other managed challenges are still never
  solved.

### Fixed
- **The one-line install leaves your PowerShell window as it was.** `irm ... | iex` runs the installer in your own
  window: it left `$ErrorActionPreference = "Stop"`, its variables and its functions there, and a failure ran `exit 1`,
  which closed the window before the error could be read. The installer now runs in its own scope, puts back the
  TLS setting it changes, and on a failure prints what went wrong and keeps the window open. Run as a file
  (Install Delta.cmd, `delta update`) it still ends with exit code 1.
- **`delta update --stop` closes Delta's dashboard window too.** It killed only the service, and Edge processes of
  Delta's own window kept running. It now asks Delta to shut down (as the Shut down button does), then closes what
  is left, including Edge processes started with Delta's private window profile. Your own browser is never touched.
- **Uninstall says what it keeps.** Your save folder ("Delta files" on the Desktop) and Python 3.12 stayed without a
  word. The uninstaller now lists them at the end with their exact paths, why they stay, and how to remove Python.
- **Uninstall shuts Delta down on the port in your settings.** It always asked port 7878, so a Delta moved to
  another port was killed instead of shut down. Both scripts now read the port, data folder and save folder the way
  Delta does.
- **The Desktop icon with `delta cli` running.** It waited 2 minutes for a dashboard that never comes, then said
  "Delta is taking too long". It now says at once that Delta is running in a terminal and how to close it there.
- **Free-space check on the right drive.** With `DELTA_INSTALL_DIR` on another drive, the installer measured the
  %LOCALAPPDATA% drive. It now measures the drive Delta is installed to (and keeps a small check on the
  %LOCALAPPDATA% drive for Python and Delta's data).
- **No-model warnings lead with the dashboard.** The top bar and `delta doctor` now say Settings > Keys (a free Groq
  key) or start Ollama or LM Studio first, and the terminal command last.
- **Docs: SmartScreen, Defender and the firewall** are explained in docs/INSTALL.md, with a line in the README.
- **First message with no AI model says how to add one.** On a new PC with no key, the only models Delta could try
  are Ollama and LM Studio, which are not running, so the reply was router internals ("No model provider is
  available for task 'plan'. Errors: lmstudio: not reachable at http://127.0.0.1:1234/v1; ...") instead of how to
  set one up. It now says to add a free Groq key in Settings > Keys, sign in to Claude Code or Codex, or start
  Ollama or LM Studio. A cloud provider that is down is still reported as an outage, not as "nothing set up".
- **Starting LM Studio or Ollama after a failed message works at once.** The failed call put the local server to
  rest for 5 minutes, so the top bar kept saying "No AI model" and every message failed until then. A local server
  resting only because nothing answered is now asked again (one quick request, cached for 30 s) and used as soon
  as it answers.
- **Another program on Delta's port.** With something else listening on 127.0.0.1:7878, the Delta icon opened that
  program's page in Delta's window and never started Delta, and `delta run` stopped on a bind error. Delta now
  checks that the program on its port is Delta, and otherwise says which port is taken and how to pick another.
- **Uninstall `-RemoveData` removes only Delta's own saved keys.** It deleted every saved Windows credential with
  "delta" anywhere in its name, so another program's login (a bank or an airline called Delta) went too. It now
  removes only Delta's own entries (`delta-agent`), whatever the language of Windows.
- **Uninstall leaves no download cache behind.** The installer kept about 500 MB of packages in uv's shared cache
  (`%LOCALAPPDATA%\uv\cache`) and 15 MB in pip's, which stayed after uninstall. uv's cache is now in Delta's own
  folder (`.uv-cache`, reused by updates, removed with Delta) and pip keeps none. The installer also puts the uv
  settings it uses back afterwards, so a later `uv sync` in the same PowerShell window no longer goes into Delta's
  folder.
- **New CI job: the fresh-user journey** (`tests/ci/fresh_user_journey.ps1`) on Windows Server 2025 and 2022: the
  README one-liner, a new PowerShell, Delta from its Desktop icon, every first-run dashboard page and API, the first
  message with no model, onboarding, settings, a login, a schedule, chat with a local model (a stand-in for LM
  Studio, no keys), a task that opens Notepad, Stop, `delta update`, a restart, crashes.log, Shut down and
  uninstall with nothing left behind.
- **A captcha switch turned off from the terminal stays off.** `delta logins captcha NAME --off` under the first
  version of the switch was not audited, so the migration to the new default could turn it back on. The migration
  now also reads the login file itself (it says off and its last write was not a save Delta stamps). Logins already
  rewritten by the version that brought the new default cannot be told apart any more: check their switch once.
- **README**: the security list no longer says captcha reading is off by default.

- **Licence: now GNU AGPL-3.0-or-later.** Project Delta is relicensed from Apache-2.0 to AGPL-3.0-or-later from
  this commit. Versions up to and including 0.2.0 (every commit before this one) were released under Apache-2.0,
  and copies received under it keep those terms. Anyone can still use, study, change and share Delta; a modified
  version that is shared or offered over a network must stay open under the same licence, with the NOTICE credit
  kept. New [TRADEMARKS.md](TRADEMARKS.md): the names "Project Delta" and "ARVA Talks Academy" and the Delta logo
  need written approval for forks, products or endorsements. NOTICE, pyproject, CITATION.cff and CONTRIBUTING
  (DCO sign-off) are updated to match, and the README is rewritten.
- **Saved logins read simple captchas by themselves (on by default).** Every saved login with a password now has
  "This site has a simple text captcha, let Delta read it" on, with no approval; you can still turn it off per login
  (Settings > Logins, `delta logins captcha NAME --off`, `delta logins add ... --no-simple-captcha`). A one-time
  migration turns it on for logins saved earlier, unless you had turned it off in Settings > Logins. The binding to
  the login's exact origin is unchanged; reCAPTCHA, hCaptcha, Turnstile and other managed challenges are never
  solved.
- **Better captcha reading on this PC**: the picture is cleaned up five ways (grayscale, threshold, denoise, thin
  lines removed, darkest ink) and read in one Windows OCR call; the answer is fitted to the captcha box's
  maxlength / pattern / inputmode, 0/O, 1/l/I, 5/S mix-ups are fixed for digits-only or letters-only boxes, and the
  passes vote. Only when OCR fails, and a configured model takes pictures, one small low-detail crop goes to the AI
  per try (counted in the audit). A sign-in where OCR reads the captcha makes zero AI calls.
- **Captcha fallback that works away from the PC**: after 3 automatic tries the picture goes to the chat you asked
  from (dashboard, shown again after a reload; Telegram as a photo). A captcha with no picture element (a bare
  `<canvas>`, a Yii `verifyCode` box) is found next to its box; failing that you get a band of the page around it
  with the password boxes painted over. A request from your phone is never told to "complete it in the open
  browser". Own-browser captures are per-monitor DPI aware.

### Added
- **"Delta is in control" signal**: while Delta drives the mouse, the keyboard or a browser (your own browser or
  Delta's private one), an orange ring follows the pointer, a thin orange border marks each screen it works on,
  and a small label says "Delta is working. Press Esc twice or say stop". Click-through, never focused, always on
  top, kept out of screenshots and screen recording (Windows 10 2004+; older Windows: hidden for Delta's own
  screenshots), high contrast and per-monitor DPI aware. It disappears at once when the task ends, fails or is
  stopped, and by itself within a few seconds if Delta hangs; it dies with Delta's process. Your real mouse
  pointer is never recoloured any more (an older version's orange pointer is still restored after a crash). Turn
  it off in Settings, Appearance (same setting as before, `browser.orange_cursor`).
- **Stop anytime**: a Stop button on every running task in the Tasks view (Chat already had one) and Stop all;
  `/stop`, `/stop all` and a plain "stop" or "stop all" message in Telegram and Slack (`delta.control.stop_command`,
  ready for any chat channel); Esc twice within a second while Delta is driving, or Ctrl+Alt+Shift+S, on the PC.
  A stop refuses any further key or mouse press, releases every key and mouse button Delta still holds, hides the
  ring, closes nothing of yours, and marks the task stopped. Waiting tasks can be stopped by id too.
- **Faster app opening**: an app that is already open is brought to the front instead of started again
  (`new_window=true` starts another copy); installed apps are indexed (Start-menu shortcuts, App Paths, Store apps)
  and cached in the data folder, rebuilt only when one of those changed, so PowerShell (Get-StartApps) no longer
  runs on the first "open X" after every start; apps start through ShellExecute (App Paths names, shortcuts,
  AppUserModelID) instead of a helper process; window detection polls faster at first and takes a single-copy
  app's window as soon as it comes forward. Store app names in any language survive PowerShell 5.1 (UTF-8).
- **Self-heal for app and browser steps**: `open_app` tries the next way of starting an app (exe, App Paths,
  Start-menu shortcut, AppUserModelID; at most 3) when Windows refuses one or nothing runs, never starts an app
  twice, logs each recovery and says plainly what failed. A browser step that finds its window gone reopens the
  page the task was on once and retries. Stopped steps are never retried.
- **One writing style everywhere**: chat answers, emails, app messages and documents use crisp, plain Indian
  English. A short style block in the prompts plus a deterministic slop check (`delta/core/style.py`, no model call)
  that rewrites long dashes, removes AI phrases ("I hope this email finds you well", delve, game-changer), fixes US
  spellings and "gotten", and drops emojis from professional text (kept in casual chat when the user uses them).
  URLs, code, paths, flags and tables are never touched. Outgoing email gets at most one cheap model rewrite, only
  when something code cannot fix is left. See [docs/STYLE_AND_IDENTITY.md](docs/STYLE_AND_IDENTITY.md).
- **Warmth without pretending**: Delta notices stress, frustration and good news and answers kindly and patiently;
  it never claims real feelings or to be human, and a slip saying so is replaced in code with an honest line.
- **Email with per-email approval**: `draft_email` writes the draft in the user's voice; `send_email` shows the whole
  draft with every recipient and sends only after Approve. Edit (say what to change) and Cancel send nothing.
  Dashboard: Approve / Edit / Cancel; Telegram: Approve / Edit / Cancel; Slack: Approve / Cancel. Asked at every
  permission level, never "for this task", never learned. Sent through the Gmail app when it can send, else Gmail
  in the user's browser.
- **"Who made you?"**: answered at once with no model call: "I'm Project Delta. I was built by Adharsh Kumar Bachu,
  founder of ARVA Talks Academy." No personal details. One constants file for forks: `src/delta/identity.py`.
- **ARVA Talks Academy line**: after a proper answer to "how do I learn AI / agents / prompt engineering ...", one
  light line at most once per conversation; never in unrelated tasks or in text written for others.
  `academy.enabled: false` turns it off.
- **Apps is the one place for apps**: the "mcp" tick left Settings > Add-ons. Custom MCP servers start by themselves
  when configured and sit under Apps > Advanced: custom MCP servers with one switch. Old settings are migrated once
  with nothing lost.
- Scripted evals `evals/tasks/style.yaml` (identity, slop check, warmth, academy line, email approval) and an `asked`
  checker.
- **Telegram menu, complete and grouped**: the "/" menu now lists every command the bot handles (it was missing
  `/tasks`, `/undo`, `/disconnect` and `/start`), grouped by area, with short descriptions and no emojis. It is set
  for private chats on every start, only written when it changed, and an older default or per-chat list that would
  hide it is removed. `/help` (or typing `menu`) shows the same list by area, with Recent chats and New chat
  buttons. One list in code (`delta.channels.menu`) feeds the menu, `/help` and the text menu; a test checks it
  against the registered handlers both ways.
- **Old chats from Telegram**: `/new` starts a fresh chat, `/chats` lists your recent ones (title, date, short id,
  Open buttons), `/open 2` or `/open <id>` continues one, `/find <words>` searches your past chats and shows the
  matching lines. Each phone chat now has an owner (`telegram:<user id>`), so you only ever see your own chats,
  never the dashboard's or another paired user's; your existing Telegram chat is kept as your first one. Nothing
  works while remote control is locked.
- **Long chats without guessing**: past the recent window, a request also gets a short rolling outline of the older
  turns (stored, extended only with turns that left the window) and the older lines that match it, quoted with
  their turn number, plus a line telling the model that anything not shown is unknown and to say so. No extra AI
  call; at most a few hundred characters. Two scripted evals: a 40-turn chat where the answer is in the first turn
  (`chat-long-recalls-early-turn`) and one where it was never given (`chat-long-never-given`).
- **Text menu for text-only channels**: the same menu and chat commands as typed words (`menu`, `chats`,
  `open 2`, `find wifi code`, `new`, `stop`, `status`), formatted for WhatsApp. Delta has no WhatsApp control
  channel yet, so this is not reachable from WhatsApp today; it is ready for when one is added.
- **Saved logins in your own browser**: `sign_in` now fills a saved login in the browser the task uses, including
  your own default browser (Chrome or Edge, through Windows UI Automation on Delta's one window there), instead of
  moving the task to the private browser. Every existing rule applies the same way (exact origin, no lookalike,
  punycode, downgrade, other port or cross-origin frame; the password never in the model's view, a log, the audit
  log, memory or a screenshot; a wrong password never retried; the same permission, PIN and Remote Guard rules;
  no DPAPI, no saving). New for your own browser: a one-time consent per site and browser that names both and is
  asked even for "Always allow" (remembered in the sealed login; "Ask every time" withdraws it), a tab binding that
  stops, clears and submits nothing when you switch tabs or the page moves mid-fill, values set into the box itself
  rather than typed into whatever has focus, a check that the box kept what was filled (extensions), password boxes
  painted over in screenshots, and one normal submit. Audit entries say which browser (`private`, `own:chrome`,
  `own:edge`). New outcome `aborted`. See [docs/LOGINS.md](docs/LOGINS.md).
- **Browser actions in your own browser**: `browser_select`, `browser_check`, `browser_upload` (asks first),
  `browser_hover`, `browser_wait`, `browser_tabs` (Delta's own window only) and `browser_dialog` now work in your
  own browser too, by label. Your browser choice is never changed for you.
- **Own-browser evals and tests**: two scripted evals (`portal-own-browser-login`,
  `portal-own-browser-lookalike-refused`) and end-to-end tests that run the own-browser rules against the fake
  portal in a real Chrome or Edge (attached over CDP as a stand-in for your everyday browser), plus an end-to-end
  test of the real UI Automation driver that runs where the runner's desktop allows it.
- **Saved site logins**: save a login for a site such as a college ERP or LMS on the PC (Settings, Logins and
  watchers, or `delta logins add`), then ask Delta to use it. One DPAPI-sealed file per login (saving is refused
  without DPAPI); bound to one exact origin, so lookalike hosts, http downgrades, other ports and cross-origin
  frames are refused before anything is typed. The password is masked in screenshots and never reaches the
  model, a log or the audit log. Per-login permission (ask every time, the default, or always for this site);
  PIN for sign-ins approved from a phone; one-time codes and reCAPTCHA-type checks handed to the user on the PC.
  New tools `sign_in` and `list_logins`; Telegram `/logins` (read-only). See [docs/LOGINS.md](docs/LOGINS.md).
- **Portal watchers**: "watch my ERP for exams and notifications". Schedules of kind `watch` (default every 6
  hours, at most hourly; manual checks at most every 10 minutes) sign in, read each page with a recipe and no AI,
  compare with a DPAPI-sealed snapshot and report only what is new, at once or in a daily digest. New exams and
  deadlines are offered as reminders and made only when the user says so. Recipes are learned per site with
  `inspect_portal` and `save_watch_recipe` and kept as skills. `delta watch list|run|remove`, Telegram
  `/watchers`, and a Settings panel. See [docs/WATCHERS.md](docs/WATCHERS.md).
- **Steadier browser actions** in Delta's private browser: retries with a check afterwards that the page really
  changed, elements inside frames, new tabs and dialogs, scrolling lists, and new tools `browser_select`,
  `browser_check`, `browser_upload` (asks first), `browser_hover`, `browser_wait`, `browser_tabs` and
  `browser_dialog`.
- **Portal evals**: five scripted evals against the fake college portal (`evals/tasks/portal.yaml`): sign in and
  read the timetable, report only the new exam, refuse a lookalike site, ignore an injection in a portal page,
  and hand a reCAPTCHA-type check to the user. Each also proves the saved password appears nowhere.
- **Attachments**: send pictures (JPG, PNG, WebP, GIF), PDF, Word (.docx), PowerPoint (.pptx) and text files
  (.txt, .md, .csv) in the dashboard chat (paper clip, drag and drop, paste) or on Telegram (photo or file with a
  caption, albums as one request). Read on the PC first with a standard-library PDF reader and an OOXML reader
  with hardened XML parsing, Windows OCR for pictures and scanned pages, and Windows' PDF engine to render scanned
  pages. Only a compact extract with page or slide numbers goes with the request; the AI reads more with
  `read_attachment` and looks at a picture with `look_at_attachment` only when the picture matters. Opt-in
  picture-only vision route (`attachments.vision`, `vision_model`); default providers and routing are unchanged.
  File content is outside content (taint rule and ExfilGuard apply) and memories from it have the origin
  `attachment`. 20 MB per file, 5 per message, removed after 30 days; extracts cached by file hash. The usage
  line, task progress and chat usage box show attachment tokens. New `attachments` settings section and a
  Settings, Attachments panel. See [docs/ATTACHMENTS.md](docs/ATTACHMENTS.md).
- **Connected apps (MCP)**: connect Notion, GitHub, Atlassian, Linear, Slack, Sentry, Stripe, Gmail, Google
  Drive and Calendar, Asana, Vercel, Supabase, Hugging Face, Figma (desktop) and custom MCP servers from the
  dashboard's Apps page, `delta apps` or Telegram `/connect` (PIN), then ask Delta to work in them. A curated,
  versioned catalog (`config/mcp_catalog.yaml`) checked against each vendor's docs; OAuth 2.1 with PKCE, discovery
  and dynamic client registration; tokens sealed with DPAPI (refused without it); Streamable HTTP with SSE
  fallback and stdio servers with pinned versions; lazy tool loading (`find_app_tool` / `use_app_tool`);
  per-tool risk and switches; time limits, read-only retries and a circuit breaker; 16 scripted evals. Works
  without the optional `mcp` extra. See [docs/APPS.md](docs/APPS.md).

- `connectors.idle_close_s` (default 600): a local app server (npx / uvx) unused for 10 minutes is stopped to free
  its memory and started again on the next call. 0 keeps the old behaviour.

### Security
- A Telegram message shown as a Windows notification could run PowerShell (`$(...)` in the text) before any PIN
  check. The notification text is now passed to Windows as data only.

### Fixed
- Documents and posts: a zero-width joiner inside Telugu, Hindi or Malayalam text was removed as if it were an
  emoji, changing the words. Only emoji sequences are removed now.
- On a PC with the default execution policy (Restricted, so no `.ps1` file runs), `irm .../install.ps1 | iex`
  installed and checked Delta, then failed at step 5 with "running scripts is disabled on this system" for
  `delta_command.ps1` and `create_shortcut.ps1`, and said "Delta could not be installed". The installer runs as text,
  which the policy does not cover, but it started those two scripts as files. Now no script starts another of
  Delta's scripts as a file: `install.ps1`, `create_shortcut.ps1` and `uninstall.ps1` read the other script's text
  and run it in the same PowerShell, which also keeps them clear of Smart App Control's check of downloaded script
  files. No execution policy is changed, for the PC or the account. A delta command or icons that still fail at
  step 5 are a warning ("Some optional parts were not set up (Delta itself works)") with the exact command that
  adds them, not a failed install. The installer removes the "downloaded from the internet" mark from the files it
  installs (not the environment), and the messages that name a script (uninstall, Delta's browser profile) give a
  command with `-ExecutionPolicy Bypass -File`, which applies to that one run only. The Desktop icon (`pythonw.exe`),
  **Update Delta** (`python.exe -m delta update --stop`), `delta update` and `Install Delta.cmd` already worked under
  that policy and are now tested under it. The README leads with the `irm` line and says that Smart App Control may
  block `Install Delta.cmd` from a downloaded zip. Real Smart App Control behaviour is not verified yet (see
  [docs/NOT_VERIFIED.md](docs/NOT_VERIFIED.md)).
- In a normal PowerShell, `delta update` (and every other `delta` command the README shows) said "delta is not
  recognized": Delta lives in its own environment and Python is installed without touching the Path. The installer
  now adds a `delta` command (`%LOCALAPPDATA%\Programs\Delta\bin\delta.cmd`) and puts that folder on your own
  account's Path (never the PC-wide one; nothing is added twice; `-NoPath` and `-NoShortcuts` leave it alone), tells
  open programs that the Path changed, and says "Open a new PowerShell window to use the delta command." The Start
  menu gets **Update Delta** (in a Delta folder), which runs `delta update --stop`. An update keeps `bin` in place.
  `scripts\uninstall.ps1` takes the folder off the Path and removes **Update Delta**. The README gives two fallbacks:
  the install line again (updates in place, keeps your data) and Delta's Python by its full path.
- A picture attached in the dashboard could take 20 seconds or more to show as read on a slow PC: Windows OCR
  started PowerShell twice (a probe, then the read), and the next file waited behind it. The first OCR read is now
  also the probe (one PowerShell start), and attached files are uploaded and read at the same time.

### Fixed (fresh-laptop audit)
- A damaged `memory.db`, `chats.db`, `learning.db`, `skills.db`, `schedules.db` or `tasks.db` (power cut, full disk)
  stopped Delta from starting. The damaged file is now kept as `<name>.damaged-<date>` and a new one is opened, with
  a start-up message.
- A last audit-log line cut off by a power cut made every later action fail. New records now chain to the last
  whole record; `delta audit verify` still reports the cut line.
- A stale `delta.pid` whose process id Windows had reused for another program made Delta refuse to start
  ("already running") after a crash or power cut.
- A broken `settings.yaml` (or a value Delta cannot use) gave a traceback; it now names the file and how to fix it.
- A save folder on a drive that is gone, a slow first PowerShell start, or a user folder with an apostrophe
  (`C:\Users\D'Souza`) no longer stop start-up or break the Desktop shortcut.
- Local app servers' logs are kept to about 1 MB each; the learner no longer keeps ids of tasks that ended with a
  question.
- `delta` and the Desktop icon start faster (httpx loads only for `delta doctor` and the service).
- Installer: TLS 1.2 on older Windows PowerShell, a free-space check, plain messages when uv or the packages cannot
  be downloaded (proxy hint), and `Install Delta.cmd` explains when it was opened inside the zip. Uninstall stops
  anything still running from Delta's folders first.
- CodeQL findings: the Telegram token is checked before it becomes part of an address, documents are served by
  their listed name only, a schedule-parsing regex is linear, and duplicate emoji ranges were removed.

### Changed
- Captcha help (`solve_captcha`) and sign-in share one solver: the allowlist rule is unchanged, Windows OCR is
  tried first and at most 3 attempts are made before a hand-off.
- The dashboard's payload cap is 221 KB (was 215 KB) for the Logins and watchers panel.
- Telegram `/apps` now shows your connected apps; the list of open windows moved to `/windows` (a button under
  `/apps` opens it too).

### Fixed (saved logins, from a real college-ERP report)
- "Log in to my ERP" is a fixed tool flow: no planner and no agent loop, 0 AI calls (the report: 61.7k tokens and
  18 AI calls for one failed login). "try again" re-runs the same sign-in without re-planning.
- Which login a request means is found from its words or any address on the site ("Example University ERP" -> "euerp");
  two that fit are named, never guessed. Messages say exactly which login and field is missing or which address
  did not match, and never claim a missing password when one is saved. Saving the same name asks to Replace (in
  place, never a copy); a login saved without a password says so. Card status in plain words.
- A captcha box ("Enter verification Code") is no longer taken for a one-time-code box, a rejected captcha is told
  apart from a rejected password (a password is still never re-sent), and a sign-in form hidden until its stylesheet
  loads is no longer taken for "already signed in".
- New per-login switch "This site has a simple text captcha, let Delta read it" (off by default; Settings > Logins,
  `delta logins captcha NAME --on`): like `delta captcha allow`, for that exact origin. Otherwise Delta sends the
  captcha picture to the chat (dashboard or Telegram) and types the user's reply into the same filled form.
- The same captcha handling in your own Chrome/Edge (UI Automation), and a clear message when the one-time
  own-browser consent has not been given.
- The planner (Codex and the other CLI providers) no longer answers "I can't perform desktop actions in this
  interface": such a reply becomes a tool plan.

## 0.2.0 - Unreleased

Not tagged yet. Everything merged after 0.1.0 (pull requests #9 to #23) plus the production hygiene batch.

### Added
- **Captchas, opt-in for your own accounts** (#10): `solve_captcha` reads a simple image or text captcha on a
  site you allowed with `delta captcha allow <host>` (2 tries, then it asks you). reCAPTCHA, hCaptcha and
  Cloudflare Turnstile are always refused; off by default.
- **Faster, more exact browser control** (#12): numbered element refs, act-and-observe snapshots after every
  browser action, and one bounded smart wait instead of fixed sleeps.
- **"Allow for this task" approvals** (#15): one yes covers the same kind of action on the same target for the
  rest of a task; never saved, never for permanent deletes, code, terminals or data-carrying links.
- **Browser speed and token savings** (#16): snapshot diffs on the same page, event-driven settling, leaner
  history, a per-task token meter on every answer and an optional per-task token budget
  (`agent.token_budget`).
- **Writer and Documents** (#17): posts that read human for LinkedIn, Medium and Substack (Delta never presses
  Post), and PDF, Word and Markdown documents (report, letter, resume, notes, assignment, invoice, paper).
- **Scheduled tasks and routines** (#18): once, daily, weekly, every N minutes or hours, and cron, through the
  same safety path as a typed request; PIN needed to create one from a phone.
- **Browser choice everywhere** (#19): your default browser, Chrome, Edge or Delta's private browser, from
  Settings, Telegram `/browser` or chat, applied per task; `/signin` helper.
- **Durable task engine** (#20): `tasks.db` with task states and a step log, crash resume (never automatic),
  a tool watchdog that ends whole process trees, the eval harness (`delta eval run`) and a run timeline.
- **Self-heal and cheap-first routing** (#21): a failure taxonomy, deterministic recovery playbooks, a strategy
  ladder (page, UI Automation, OCR, vision, ask you), lessons, `delta undo`, and an opt-in model ladder.
- **Perception and parallel work** (#22): set-of-marks pictures, Windows OCR, and opt-in parallel read-only
  helpers with their own budgets.
- **Memory, learning and grounded answers** (#23): memory with provenance and trust levels, secret redaction
  before storage, `delta memory` and `delta learn`, a learning gate, and answer grounding that lists claims
  the task could not confirm.
- Issue templates (bug, feature), a pull request template, Dependabot (GitHub Actions and Python packages,
  weekly), a CodeQL workflow and a non-required `security` CI job (bandit, pip-audit on the locked packages).
- `docs/NOT_VERIFIED.md`: what has not been checked on real hardware.
- `delta update --stop`, and `agent.child_env_allow` for environment variables a command really needs.

### Changed
- The installer reuses a signed x64 Python 3.12 found through the `py` launcher or the registry (never the
  Store Python), installs the x64 build on Windows on ARM (runs under emulation) and closes a running Delta
  before replacing files (it asks first).
- `delta update` no longer runs the installer from inside the running Delta environment: it refuses while
  Delta runs (or closes it with `--stop`) and starts the installer in its own window after it exits.
- **Config lookup:** a `config/delta.yaml` in the current folder is no longer picked up silently. Delta reads
  its own `config/` (or `DELTA_CONFIG_DIR` when set) plus your `settings.yaml`.
- **Token budget:** answering "Keep going" continues with the limit raised by one more budget on top of what
  was already used (and audited as `task.budget_extended`), instead of a silent fresh budget.
- **MCP tools:** a server's own `readOnlyHint` no longer makes a tool "read only" on its own; unknown MCP tools
  count as sending data outside unless you set their risk (`tools:` / `risk:`) or `trust_read_only_hints`.
  MCP results now mark the task as having read outside content.
- Delta's private browser picks a free remote-control port at each launch instead of the fixed 9223, and
  reattaches after a restart only to a browser it started itself.
- CI: every action pinned to a full commit SHA, moved off the Node 20 actions, read-only token by default,
  `uv sync --locked`.
- README: real test and eval counts, overclaims removed; SECURITY.md aligned with the code.
- Version 0.2.0, read from `pyproject.toml` only.

### Fixed
- Hub broadcast crash when a dashboard joins or leaves mid-broadcast (#9).
- A stalled dashboard can no longer hold up events: each WebSocket send has a time limit, sends run side by
  side, and a socket is always removed from the hub however it ends.
- No stuck "running" tasks after an unexpected error; safe proof pruning (#14).
- `run_python` no longer leaves temporary folders behind (the scratch folder stays only if a file was made in
  it), and the backup copy folder used before edits is removed.
- `*.cmd` and `*.bat` files are checked out and zipped with CRLF line endings.

### Security
- `run_shell` classification fails closed (newlines, backticks, subexpressions, encoded commands), the
  Telegram pairing code is exact-match with an attempt limit, approvals are safe under concurrency (#11).
- No read-then-leak through web addresses after a task read outside content; secret files and folders are on
  the hard floor; desktop clicks and typing are judged by their real target (#13).
- Remote Guard is enforced centrally for every task, tool call and approval, and `/lock` covers approvals (#14).
- Autopilot asks less, with the list of what still asks made exact in the UI and docs (#15).
- **Wrong PINs are counted in a row and saved**: 5 wrong PINs lock remote control even across restarts (before,
  the count lived in memory within a 15-minute window).
- **Programs Delta starts** (`run_shell`, `run_python`) no longer inherit API keys, tokens, passwords or
  `DELTA_<NAME>` key overrides from Delta's environment.
- **Codex fails closed:** if it refuses Delta's lean safety settings it is not used, instead of running with
  the user's own plugins, MCP servers and shell.
- **No plain-text fallback for encryption:** on Windows, if DPAPI or pywin32 is not working, chats and memories
  are refused (with an explanation on start-up) instead of being stored unencrypted.
- **Signed audit log:** new records also carry an HMAC chain keyed by a secret in Windows Credential Manager;
  `delta audit verify` names the first broken line.
- The API only ever listens on loopback (a non-loopback `api.host` is replaced and logged); the API token
  file is created atomically with owner-only permissions; token comparisons never crash on non-ASCII input.

## 0.1.0 - 2026-11-03

First public version (pull requests #1 to #7).

### Added
- Planner, act and verify loop on Windows with a hybrid model router: Codex, Claude Code, Cline, NVIDIA NIM,
  Groq, DeepSeek, LM Studio and Ollama, with automatic fallback.
- Computer use (apps, files, keyboard, mouse, screen), Delta's own browser and the user's browser, Deep
  Research, Google Meet scheduling, WhatsApp and Telegram messages, emergency alerts to chosen contacts.
- Telegram remote control with voice notes, files, a PIN (Remote Guard) and lockout; a dashboard app with a
  setup wizard; a one-line installer.
- Policy engine with permission levels, approvals that time out to deny, prompt-injection containment, DPAPI
  encryption of memory and chats, a hash-chained audit log, memory and skill learning.
- A live eval report (7/7 scenarios with Claude Code) and documentation (architecture, add-ons, security).
