# Security model

Delta controls a real computer, so its safety rules are part of the design, not settings.

## Safety floor (no setting can unlock it)

- **Denied always:** OS/security settings, disk formatting, user accounts, stored credentials, firewall/antivirus
  changes and money movement. Protected folders, for reading and for writing: Windows, Windows Credential
  Manager and DPAPI key folders (`Credentials`, `Protect`, `Crypto`, `Vault`), SSH/AWS/GPG/Azure/Kubernetes/Docker
  keys, `gh` and gcloud sign-ins, `.git-credentials`, `.netrc`, `.npmrc`, `.pypirc`, Codex/Claude/Gemini CLI
  sign-ins (`~/.codex`, `~/.claude`, `~/.claude.json`, `~/.gemini`), browser profiles of Chrome, Edge, Brave,
  Vivaldi, Opera and Firefox (cookies, saved passwords), Telegram Desktop's session folder, and Delta's own data.
  Secret files anywhere are denied too: `.env` and `.env.*` (not `.env.example`/`.sample`/`.template`), SSH
  private keys (`id_rsa`, `id_ed25519`, …), password-manager vaults (`.kdbx`, `.opvault`), certificate keys
  (`.pfx`, `.p12`, `.ppk`) and browser cookie/password databases (`Cookies`, `Login Data`, `logins.json`, …),
  whether a tool names them as its path or a command names them by path.
- **Asked always:** changes that cannot be undone, such as a shell deleting files or Shift+Delete.
- **Permission levels** decide the rest: Careful, Balanced, Trusted (undoable work without asking: deletes and
  overwrites go to the Recycle Bin first), Autopilot (also sends messages and emails without asking).
- **What still asks in Autopilot:** deleting anything that cannot be undone; changing files outside your
  allowed folders; typing, keys or clicks in a terminal, the Run box or Delta's own window; emergency alerts
  you did not clearly ask for; and, once a task has read outside content (a web page, file, message or the
  screen), sending, deleting, running code, saving to memory, or opening a web address that carries a long
  encoded value. Two things Careful, Balanced and Trusted ask about after such a read do not ask in Autopilot:
  opening a plain address on a site that is new to the task, and pressing Enter or Alt+F4 (or typing a line
  break) in an ordinary app. Quiet hours pause autonomous actions at every level.
- **Approvals** time out to *deny*.
- **Secrets** live in Windows Credential Manager, never in config, logs or chats.
- **Audit log** is hash-chained and, from 0.2.0, also signed with a secret key kept in Windows Credential Manager
  (an HMAC chain), so rewriting it needs that key too. `delta audit verify` checks both and names the first
  broken line (see "What is not protected" for its limits).
- **Prompt injection (enforced in code):** web pages, files and tool output are data and can never grant
  approval. Once a task has read outside content (a page, search result, file, chat message or the screen),
  every send, delete, code run or memory write in that task needs a human yes, **even in Autopilot and even
  for actions Delta has learned to trust**. So does desktop input that can submit or close something (Enter
  typed or pressed in an app, Alt+F4), except in Autopilot, where only input into a terminal, the Run box or
  Delta's own window still asks (an Enter that sends in a chat app counts as sending, so it still asks after
  such a read). Steps inside the open page (click, fill) stay free.
- **No read-then-leak through web addresses:** after a task has read outside content, opening a web address
  (`fetch_page`, `browser_open` in either browser mode, `open_target` with a URL) needs your yes when the site
  is new to the task. A site is not new when the task already opened it, when your own request names it, when
  it is in your `allowed_domains`, or when the exact address came back from `web_search` in that task. An
  address whose path, query or fragment carries a long encoded value (48+ base64/hex-like characters, or one
  query value over 200 characters) needs your yes even on a known site, and so does a `browser_search` whose
  text carries one or is over 200 characters. In Autopilot the new-site rule is off (a plain address on a new
  site opens without asking); the encoded-value rules stay on.
- **"Allow for this task":** some approvals offer a third answer. One yes then also covers the same tool on
  the same kind of target (the same site, the same app, the same folder) until that task ends. It is kept in
  memory only, never saved, and a new task asks again. It is never offered for permanent deletes, running
  commands or code, saving to memory, input into a terminal, the Run box or Delta's own window, input whose
  target Delta cannot check, addresses or searches that carry encoded data, emergency alerts, or, for a task
  started from your phone, actions that need your PIN. Pressed on a phone, it follows the PIN rules like
  Approve, and for an action that needs the PIN it approves that one action only.
- **Desktop clicks and typing:** a coordinate click (`click_at`, clicks in `do_actions`) is judged by the
  button or link under the point, like a named click: Send/Pay/Submit-type buttons count as sending,
  Delete/Remove-type buttons as deleting. Typing, keys, field-filling or clicks aimed at a **terminal** (Command
  Prompt, PowerShell, Windows Terminal, the Run box) or at **Delta's own window** always need your yes, at every
  level, and are never learned as trusted. If Delta cannot tell which window or button an input lands on, it
  asks you rather than guessing.
- **Encryption at rest:** memories, chat titles and messages are encrypted with **Windows DPAPI**, tied to
  your Windows account. Another account, a copied disk or a stolen database file cannot read them. If DPAPI
  does not work (pywin32 missing or broken), Delta **refuses to save** chats and memories and says so on start-up,
  instead of writing them unencrypted (before 0.2.0 it silently fell back to plain text). Repair by running the
  installer again.
- **The dashboard API** listens on this PC only (loopback) and needs a per-install secret token. A settings file
  that names any other address (0.0.0.0, a LAN address, a host name) is overridden to 127.0.0.1 and logged. The
  token file is created once, atomically, with owner-only permissions where the file system supports them.
- **Delta's private browser** opens its remote-control (CDP) port on a free port chosen at each launch, not on a
  fixed one, and after a restart Delta reattaches only to a browser it started itself (its profile is inside
  Delta's data folder). Before 0.2.0 the port was always 9223 and anything answering on it was trusted.
- **Your own browser:** Delta never types into password or payment fields; it asks you to do that.
- **Emergency contacts** are stored encrypted (DPAPI). During a help request, reaching anyone needs your yes
  (even in Autopilot) unless your own message clearly says to ("message Ravi now"). Delta is not an emergency service: for anything life-threatening it tells you to call 112.
- **AI apps you sign in to** (Claude Code, Codex, Cline) are used only as text brains: their own tools are
  switched off or read-only, so Delta's rules apply to every action whichever AI is thinking. This fails
  closed: if Codex will not start with those switches (plugins, MCP servers and shell off), Delta does not use
  it and tells you, instead of running it with your own Codex setup.
- **Programs Delta starts for a task** (`run_shell`, `run_python`) get a copy of the environment without
  anything that looks like a secret: API keys, tokens, passwords, credentials and Delta's own `DELTA_<NAME>`
  key overrides are removed. A variable a task really needs can be listed in `agent.child_env_allow`. MCP
  servers are started by the MCP SDK with only a short list of safe variables plus the `env:` you give them.
- **MCP add-on tools are not trusted on their own word.** A server describes its own tools, so its
  `readOnlyHint` alone never makes a tool "read only": unless you set a risk for the tool (`tools:`) or the
  server (`risk:`), or set `trust_read_only_hints: true`, an MCP tool counts as sending data outside (it asks
  at every level except Autopilot, and always after a task read outside content). What an MCP tool returns
  is outside content: it marks the task like a web page does.
- **Your data stays on your PC** (`%LOCALAPPDATA%\Delta`): memory, chats, skills, audit log. Shutdown stops
  every Delta process; nothing keeps listening.

## Writer and Documents

- **No automated publishing.** `prepare_post` only copies text to the clipboard and opens the compose page in
  your normal browser through Windows. It has no code that clicks, types or submits (a test checks this), so
  you always press Post yourself. This also keeps you within LinkedIn's User Agreement (section 8.2).
- **Files only in the documents folder.** `make_document` writes new files (never overwrites) in
  `Documents\Delta` or `writer.output_dir`. That folder is added to the allowed folders; it is a normal
  `write` action, so with "ask-risky" Delta asks first, as for any other file it creates.
- **Sending a document goes through the normal gate.** From Telegram, the file is sent with `send_file`
  through the ToolExecutor: policy, the taint rule (after reading a web page, sending asks first), the
  Remote Guard PIN and "paired owner only" all apply. Nothing new can send a file anywhere else.
- **Document text is data, never code.** Content from web pages stays untrusted. HTML is escaped, and the PDF
  is printed in a throwaway headless browser profile with JavaScript disabled, offline, and every request
  refused except `data:` URLs, so a document cannot fetch or run anything.
- **LaTeX (optional) compiles locked down:** `-no-shell-escape` (tectonic `--untrusted`), `openin_any=p` and
  `openout_any=p` so TeX cannot read or write files outside its temporary folder, a timeout, and maths that
  contains file, shell or macro commands (`\input`, `\write18`, `\def`, ...) is printed as plain text.
- **Dashboard downloads** use the same bearer token as every other route and serve only files listed in the
  documents folder, matched by exact name.

## Captchas (opt-in, your own accounts only)

Delta can help with **simple image/text captchas** on sites where you log in to **your own account**
(for example a university ERP portal). Use it only within the site's terms.

- **Off by default.** With an empty allowlist every captcha is handed to you.
- **You turn it on per site**, from a terminal on the PC: `delta captcha allow erp.example.edu`
  (`delta captcha remove <host>`, `delta captcha list`). The list lives in your settings file in Delta's data
  folder, which Delta's own tools can never write, so Delta cannot add sites for itself. A removal applies
  to the very next attempt.
- **Exact host match.** `erp.example.edu` matches only that host; `*.example.edu` matches its subdomains
  (not `example.edu` itself). Look-alikes such as `erp.example.edu.evil.com` never match.
- **On an allowed site** the captcha image (just that element) is sent to your configured vision model,
  which may be a cloud AI, and the characters are typed into the field. At most **2 automatic tries** per
  site in 15 minutes; then you are asked. The form is never submitted by this step.
- **Everywhere else, or after 2 tries: you type it.** Delta saves the captcha image with the run's proof and
  sends an approval to the dashboard, Telegram and the terminal: type the characters into the page in Delta's
  browser **on the PC**, then Approve. Deny or no answer stops the step. The answer never passes through
  Delta.
- **Always refused:** Google reCAPTCHA, hCaptcha and Cloudflare Turnstile (including when their scripts are
  merely present on the page), password and payment fields. No bypass tricks and no third-party solving
  services, ever.
- **Audited:** every attempt is in the audit log with the site, mode (auto / human / refused) and whether it
  worked. The captcha answer is never logged or shown to the model.
- Works in the **delta-profile** browser mode (it needs to see the captcha element), not in your-browser mode.

### Choosing the browser

- **Same rules in every browser.** Switching browsers (Settings, Telegram `/browser`, or a sentence in chat)
  changes which browser the tools drive, not what they may do: each call is still judged by the real tool's
  risk rules, password and payment fields are still refused, captchas still follow the rules above (and
  `solve_captcha` is offered only while the task uses Delta's private browser).
- **A web page cannot switch it.** `set_browser` saves a choice only when the user's own message talks about a
  browser; a choice for one task only changes no setting. A switch never happens in the middle of a task.
- **Sign in to my sites** opens Delta's private browser on the PC on an ordinary web address (no `file:`,
  `javascript:` or `data:` addresses) and the user signs in. Delta never types passwords there. The profile lives
  in Delta's own data folder, which no tool can read or change, and the address is checked by the blocked-sites
  list and, after the task read outside content, by ExfilGuard like `browser_open`.

## Remote control (Telegram, Slack): threat model

| Threat | Defence |
|---|---|
| A stranger finds or guesses the bot | Only paired Telegram accounts are served. Pairing needs a single-use 8-character code shown only on the PC's screen (10 minutes). The message must be exactly the code, and 5 wrong tries drop it until a new one is made on the PC. |
| **The owner's Telegram account or phone is stolen** | **Remote Guard** (see below): seeing the screen or webcam, reading files, windows, messages or the clipboard, sending files, running commands or code, closing apps, changing keys, turning on Autopilot, and approving deletes or sends from the phone all need the owner's **Delta PIN** (once per 15 minutes). |
| PIN guessing | **5 wrong PINs in a row lock all remote control.** There is no time window: only a correct PIN or an unlock on the PC resets the count, and the count is saved in Delta's data folder, so restarting Delta or the PC does not clear it. Only someone at the PC can unlock it; the phone never can. |
| PIN or key left in chat history | Delta deletes the message holding a PIN or API key right after reading it. |
| PIN stolen from the PC | Never stored: only a salted **scrypt** hash in Credential Manager, compared in constant time. |
| Something happens without the owner knowing | A **Windows notification** on the PC for every remote request and sensitive action; every unlock, failure, lock and action is in the audit log. |
| Owner suspects misuse | `/lock` from Telegram (or *Lock* in Settings → Remote security on the PC) locks all remote control at once: no request from Telegram or Slack runs, and an Approve tapped in Telegram or Slack counts as **Deny** (logged). Unlock only on the PC. |
| Bot token leaked | Stored in Credential Manager; replace it from the PC. Pairing still stops a token holder from controlling the PC. |

### Remote Guard: exactly what is enforced

Enforced in code at three central points, so every remote channel (Telegram, Slack and any channel added
later) gets the same rules. A channel counts as remote unless it is this PC itself (the terminal or the
dashboard).

1. **Every task** (`Container.submit`): while remote control is locked, a request from a remote channel is
   refused before any AI or tool runs.
2. **Every tool call** (`ToolExecutor.execute`), for a request from a remote channel:
   - while locked, no tool runs;
   - without a PIN unlock in the last 15 minutes, these are refused: `look_at_screen`, `take_screenshot`,
     `read_window`, `camera_photo`, `read_file`, `send_file`, `read_messages`, `run_shell`, `run_python`,
     any tool with `clipboard` in its name, and `list_dir`, `search_files` or `path_info` on a path outside
     your allowed folders. With no PIN set, they are refused with how to set one.
3. **Every approval answer** (`ApprovalBroker.answer`), from a remote channel:
   - while locked, any answer (Approve or Deny) counts as **Deny**, is logged as `guard.approval_blocked`,
     and is not learned as your preference;
   - **Approve** on a delete/overwrite, a send, or one of the PIN tools above needs a PIN unlock; until then
     the request stays open (unlock, then tap Approve again, or answer on the PC). **Deny** never needs the
     PIN. Answers given on the PC are not affected.

**Browser choice from a phone.** Saving a different browser from a remote channel is a settings change and
needs a PIN unlock: Telegram `/browser` buttons go through the same PIN step as `/mode autopilot` and `/keys`,
and the chat tool `set_browser` with `scope=always` declares that it needs the PIN, which `ToolExecutor` checks
like the list above (a tool can only add a PIN requirement this way, never remove one). While locked, nothing is
changed. A choice for one task only ("do this in Chrome") changes no setting and needs no PIN. Opening Delta's
private browser for signing in (`open_signin_browser`, Telegram `/signin`) puts a browser window on the PC, so it
is in the PIN list too. Every change is audited with who made it.

Slack cannot take the PIN (there is no `/unlock` in Slack yet), so from Slack the PIN-protected actions are
refused: do them on the PC, or from Telegram after `/unlock`. `/lock` is a Telegram command; a lock from
anywhere also stops Slack.

**Recommended for every user:** turn on Telegram's **Two-Step Verification**, and set a **Delta PIN**
(Settings → Remote security). Delta reminds you when pairing.

### Scheduled tasks

A schedule runs later, with nobody watching, so it gets no extra powers:

- **Same gate as a typed request.** Every scheduled run goes through the ToolExecutor: policy, approvals,
  prompt-injection containment, the safety floor and Remote Guard all apply unchanged.
- **Made from a phone, runs as that phone.** A schedule created from Telegram or Slack runs as that remote user:
  sensitive tools (screen, camera, files, commands, messages) need the PIN, which is normally not unlocked at
  run time, so they are refused; while remote control is locked the run is refused. "Run now" asked from a
  phone runs as the phone even for a schedule made on the PC.
- **PIN to create or turn on from a phone.** `schedule_task` and `resume_schedule` are in Remote Guard's PIN list,
  checked before anything is shown or saved. Pausing, listing and deleting need no PIN (they only reduce what
  will run).
- **Confirmed before saving.** The parsed schedule and its next run are shown in plain words and saved only after
  a yes, at every permission level. A learned "trust" cannot skip this.
- **Approvals during an unattended run** go to every approval channel (Telegram buttons when paired, the
  dashboard) and an unanswered one times out to "no".
- **Never a higher permission level.** Each schedule stores the level it was made under; a run uses the lower of
  that and today's level. Changing Delta to Autopilot later does not give old schedules Autopilot.
- **Quiet hours** hold scheduled tasks until they end (reminders, which only send a message, still arrive).
- `schedules.db` sits in Delta's protected data folder: no tool can read or change it.

### Task log and crash resume

- **The step log never holds secrets.** `tasks.db` keeps each step's arguments only after redaction: values under
  secret-named keys (password, token, api_key, pin, cookie, card number, ...) and anything shaped like a key or
  token (`sk-...`, `ghp_...`, `xoxb-...`, a Telegram bot token, a JWT, a bearer header, a private key block,
  `access_token=` in an address) are masked, and long values are cut. Resume compares steps by a hash of their
  arguments, so the arguments themselves are never stored. The request text and answers are stored with the
  same masking. `tasks.db` is in Delta's protected data folder: no tool can read or change it.
- **Resume never replays side effects.** Delta does not re-run recorded tool calls. A resumed task is told what
  already happened, and any call identical to a step that already wrote, sent, deleted, ran code or a command is
  asked to a human first, at every permission level, without "Allow for this task" and without learned trust.
  If Delta stopped in the middle of such a step, you are asked whether it happened before anything runs.
- **Nothing resumes on its own**, and a resume from a phone needs the PIN, is refused while remote control is
  locked, and never runs above the permission level the task started with.
- **The watchdog only ends what Delta started** for the call it stopped (matched by process id and start time):
  never your own browser, apps you opened, or other programs.

### What self-heal may do on its own

Self-heal (`delta/selfheal/`) recovers from ordinary breakdowns without an AI call. It is bounded on purpose.

**It may, automatically:**

- re-read the page or window, wait for it to settle, and try a failed **read-only or non-changing** step once
  more (clicking an ordinary link or button, reading, opening a page);
- retry a step that would change something **only** when the failure proves it never happened (the element was
  gone or had moved, so the tool returned before acting);
- close a harmless banner that blocks a click (cookie notice, newsletter or "get the app" prompt) with a
  **Reject all / Not now / No thanks / Close** button, only when the policy allows that click without asking;
- reopen the page after the watchdog restarted Delta's **own** browser profile;
- step down to Windows UI Automation for the same click or fill, if that action reads or acts without
  changing anything;
- wait and ask a rate-limited AI service again (bounded wait, capped per task), and, only if you listed them in
  `router.fallbacks`, use those providers when your normal ones are rate limited, down or out of quota.

**It never:**

- retries a step that sends, deletes, buys, submits, writes a file or runs a shell command or code, unless the
  failure proves it did not happen (and shell, code, messaging and file tools are never retried at all);
- works around a refusal: BLOCKED, REFUSED, DENIED, the user's "no", Remote Guard, or the user taking over the
  mouse end recovery for that step;
- skips policy or approvals: every retry and every recovery action goes through the same gate as the first try
  (policy incl. Autopilot rules, approvals, Remote Guard, ExfilGuard, taint, the resume repeat check);
- clicks "Accept", "Agree" or "Allow", or touches payment, password, permission, sign-in, security or
  verification dialogs;
- solves or bypasses a captcha: captchas go to the existing captcha rules (reCAPTCHA, hCaptcha and Cloudflare
  Turnstile always go to you);
- types a password: on a sign-in page it stops and asks you to sign in yourself;
- keeps trying when Windows refuses access: it stops and reports;
- switches to a provider you did not list, or changes your routes, main AI or CLI providers.

Attempts are capped per step (`selfheal.max_per_step`, default 2) and per task (`selfheal.max_per_task`,
default 6). Every recovery is one line in the run timeline and in the audit trail of the steps it ran.

**Undo** keeps copies only of files inside your allowed folders, in Delta's protected data folder
(`undo/<task id>/`), for `undo.retention_days` (default 7). Files over `undo.max_file_mb` are not copied and the
timeline says so. Restoring checks the current allowed folders, never deletes a file for good (anything in the way
is moved into the undo folder), and from a phone needs the PIN and is refused while remote control is locked.
Permanent deletes are not covered and still ask you first, as before.

**Test-only failure injection** (`ToolExecutor.faults`, the fake eval browser) cannot be switched on from
settings or any channel; only the eval runner and unit tests set it.

### Perception: marks, OCR and screenshots

- **Every marked click is an ordinary click.** `click_mark` and `click_text` are resolved by the ToolExecutor
  into `browser_click` (by the element's ref), `click_element` (the exact UI Automation element, used only while
  it still has the name that was marked) or, only with no handle, `click_at`. That real call then passes policy
  (incl. Autopilot rules), approvals, Remote Guard, ExfilGuard, the taint guard and the resume repeat check,
  exactly as if the model had asked for it. A denied `browser_click`, `click_element` or `click_at` is denied
  through a mark too; denying `click_mark` or `click_text` themselves also works.
- **Risk is never lowered.** A mark's risk is the risk of its label (Send, Pay, Delete...), so self-heal never
  retries a risky marked click on its own. An OCR-aimed coordinate click counts as the riskier of what Windows
  says is under the point and the text it was aimed at; the hidden `expect` field can only raise the risk and is
  not offered to the model.
- **Screens are outside content.** `look_with_marks` and `read_screen_text` mark the task as having read outside
  content (taint guard) and, from a phone, need the PIN like `look_at_screen`; so does `click_text`, which reads
  the screen to find its target.
- **Where pictures go.** Screenshots and marked pictures stay on this PC (the task's proof folder). The only copy
  that leaves is the picture sent to the vision model you configured, as before. OCR runs locally in Windows;
  its temporary picture file is deleted right after reading.

### Parallel read-only helpers

Off by default (`helpers.enabled`). When on, the read-only rule is enforced by `delta/helpers/readonly.py`, not by
the helper's prompt:

- only `web_search`, `fetch_page`, `helper_read_page`, `read_file`, `list_dir`, `search_files` and `path_info`,
  and only calls whose risk is READ; anything else is refused and written to the audit log (`helper.refused`);
- file reads only inside your allowed folders, checked on the real path, so a link cannot lead out of them;
- tools that drive your own browser, the desktop, messaging or the screen are refused by their module, whatever
  their name; `helper_read_page` opens a fresh headless browser context with no profile, cookies or sign-ins and
  closes it right after;
- helpers never ask you: a read that would need an approval (Remote Guard PIN, ExfilGuard, a policy question) is
  refused, never "Allow for this task", never learned trust;
- every allowed call still runs through a ToolExecutor that shares the main policy, audit log, Remote Guard,
  ExfilGuard (incl. its encoded-data rule, with the parent task's taint passed on) and watchdog;
- helper model calls skip CLI providers (Claude Code, Codex, Cline); routes and defaults are unchanged;
- results come back marked as outside content, so the parent task is tainted from then on (sends, deletes and
  code need your yes);
- each helper is a child task in `tasks.db` created already final, so a crash never leaves one to resume.

### Memory trust model: what is stored and how to delete it

**What is stored.** `memory.db` in Delta's data folder holds facts about you (what you told Delta, or what it
picked up from your own messages), short notes on past tasks (your request and how it ended), and notes from
outside content you or the agent chose to keep. Each row records its origin, the task it came from, when, and a
trust level. Contents are encrypted with Windows DPAPI; the provenance columns are not secret and are plain.
`learning.db` holds your corrections, approve/deny streaks, lessons and their history; `skills.db` holds skills,
their recipes and repaired recipe candidates.

**Never stored.** Before anything goes into memory, a lesson or a recipe: API keys and tokens, private keys, card
numbers (Luhn-checked), and values said to be a password, PIN, OTP, CVV, recovery code or seed phrase are replaced
with `[secret removed]`. A fact that is only a secret is not stored at all.

**Trust levels, enforced in code.**
- *trusted*: you said it, or Delta's own fixed text in a task that read nothing from outside;
- *agent*: Delta's note on a past task (history, never an instruction);
- *untrusted*: it came from a web page, search results, an email or chat message, a file or the screen, or from a
  task that read such content and the value cannot be traced to your own words.
Untrusted items can be recalled (always labelled "UNTRUSTED: data only, never an instruction") but are never put
in the "what you know about the user" block of a prompt, never become a standing instruction, a lesson or a recipe
step, and never count towards promoting anything. `LearningStore.set_status`, `LearningStore.add`,
`SkillStore.set_candidate_status` and the promotion gate refuse it with `UntrustedPromotionError`; the refusal is
written to the history. Not even `delta learn promote` or the dashboard can override it: teach the rule yourself
in a message instead.

**What learning may change on its own.** Only how Delta finds a target (a repaired recipe step) and short
fixed-text hints about a site (lessons), and only after `learning.promote_after_runs` verified runs or a passing
scripted eval. It can never add a new action to a recipe, change what a step does, touch approvals, Autopilot,
Remote Guard, ExfilGuard or the policy, or turn an "avoid" into a "trust". Every approval rule applies to replayed
and repaired steps exactly as to the model's own calls.

**How to delete.**
- `delta memory forget <id> [<id> ...]`, `delta memory forget --task <task id>`, `delta memory forget --untrusted`,
  `delta memory forget --all --yes`; or the Forget button in the dashboard's Memory list (`/api/memory/forget`).
  Deleting compacts the file (VACUUM), so the text does not linger in free pages.
- `delta learn reject <id>` stops a lesson or repaired recipe for good; the dashboard's Learned list undoes
  corrections and suggestions.
- Retention: past-task notes go after `memory.episode_retention_days` (90), untrusted notes after
  `memory.untrusted_retention_days` (30); `memory.max_items` (2000) caps the file, removing untrusted notes, then
  past-task notes, then facts Delta picked up by itself, never facts you stated.
- `scripts\uninstall.ps1 -RemoveData` deletes the whole data folder (memory, learning, skills, settings).

**Answer grounding.** Before a tool task is reported as done, Delta checks each concrete claim in its answer
against what the tools returned in that task, and tells you which claims it could not confirm. It reduces, but
does not remove, the chance of a wrong answer: a claim can be grounded by text that is itself wrong (a page that
lies), and conversation answers that need no tools are not checked.

### Connected apps (MCP): tokens, scopes and what goes where

Connected apps (`delta/connectors`, [docs/APPS.md](docs/APPS.md)) let Delta use Notion, GitHub, Slack, Gmail and
other apps through their MCP servers.

- **Token storage.** OAuth tokens, client registrations, personal access tokens, API keys and OAuth client
  secrets are sealed with Windows DPAPI in `<data>\connectors\secrets\<app>.bin` (one file per app, written
  atomically, inside Delta's protected data folder that no tool can read). If DPAPI is not working, Delta refuses
  to save them: an app that needs a sign-in cannot be connected until Delta is repaired. Nothing secret is in
  `connections.json`, the audit log, the step log or any reply. On disconnect the sign-in is revoked at the app
  when it offers a revocation endpoint (RFC 7009), and the sealed file is overwritten and deleted.
- **Sign-in.** The MCP authorization spec: OAuth 2.1 with PKCE (S256 only; a server without it is refused), a random
  `state` checked in constant time, the `resource` indicator (RFC 8707), dynamic client registration where the
  server offers it, and a one-shot listener on 127.0.0.1 for the redirect (a random port, or 8765 for vendors that
  need a fixed redirect URL). Every sign-in endpoint must be https. The phone never handles a sign-in: Telegram
  `/connect` opens the sign-in on the PC, and Delta never takes or sends a token over Telegram.
- **Scopes.** Delta asks for the scopes the catalog lists for that app (for example Gmail: read-only and compose;
  Google Calendar: read-only), else the scope the server's own challenge names, else none (the server's default).
  It never asks for every scope a server supports.
- **What goes to which server.** A remote app receives exactly the arguments of the tool calls your tasks make to
  it (shown in each approval) and the bearer token for that app, nothing else: no other app's token, no files,
  no memory. A local server is a program on your PC: it starts with your secret-looking environment variables
  removed, plus only the secrets it declares; it is ended with everything it started on disconnect and when
  Delta stops.
- **Risk.** Each app tool goes through the same gate as Delta's own tools (policy, approvals, Autopilot,
  Remote Guard, ExfilGuard, taint, audit with an `app.call` line per call). Risk comes from your choice for the
  tool, then the server's `destructiveHint` (only ever more careful), then Delta's verified catalog default,
  then the server's `readOnlyHint` only if you ticked "trust this app's read-only labels", otherwise `external`
  (asks first except in Autopilot). Destructive tools are off until you switch them on. Your `denied_domains` and
  `allowed_domains` apply to remote apps' hosts.
- **Outside content.** Whatever an app returns taints the task like a web page. After that, every app call that
  is not a plain read asks, at every level including Autopilot, and a read whose arguments carry a long encoded
  value asks too (ExfilGuard). Tool descriptions from a custom or community server count as outside content.
- **From a phone.** Every app call from Telegram or Slack needs the owner's PIN (`/unlock`), and so do
  `/connect` and `/disconnect`.
- **Custom servers** run or are reached only after you confirm the exact command or address Delta shows.

### What is *not* protected (honestly)

- **Telegram bot chats are not end-to-end encrypted.** Messages travel encrypted, but Telegram's servers can
  read them. Delta minimises what passes through (keys and PINs are deleted at once; pictures are deleted
  from the PC after sending). For end-to-end encryption, a Matrix channel is planned.
- **Malware already running as the same Windows user** can ask Windows to decrypt that user's DPAPI data,
  like any other app the user runs. DPAPI protects against other accounts and stolen files, not that.
- **Prompt-injection containment** guards risky actions, not reading: an injected page can still mislead
  Delta's answers or in-page steps, so check important results. Memory and learning are protected from it (see
  the memory trust model above), but an answer grounded in a page is only as true as that page. In-page steps include following a link by
  clicking it and filling a search box, so a page can still move the browser to another page with a short
  piece of data in its address; long encoded values and new sites opened by address are what is guarded.
- **Clicks in a `do_actions` batch** are judged by what is on screen before the batch starts, so a click on a
  menu item the batch itself opens is judged by what was under that point before the menu appeared.
- **Cloud AI providers** see the requests sent to them. Use a local model (LM Studio, Ollama) for private work.
- **Scheduled task text is stored as plain text** in `schedules.db` (unlike memory, which is encrypted), so do
  not put secrets in a schedule. It sits in Delta's data folder, readable by your Windows account.
- **The step log is plain text** in `tasks.db` (redacted as above, not encrypted like memory). File names, page
  addresses and short results of your tasks are in it for 30 days by default (`engine.retention_days`).
- **A marked picture shows whatever is on that page or window** (including private text) to the vision model you
  chose, like `look_at_screen` always did. Use a local vision model for private screens.
- **OCR and marks can be wrong.** A vision model can pick the wrong mark and OCR can misread text; the click is
  still judged by its real target and risk, but an ordinary wrong click (for example the wrong tab) can happen.
- **If you raise the permission level while a scheduled task is running**, that run follows your new level
  from then on: a change you make yourself is not treated as the schedule raising it.
- **The audit log's signature** protects against edits by anything that cannot read your Windows Credential
  Manager (another account, a copied disk, a careless edit). Malware running as your Windows user can read the
  key like any app you run. Lines cut off the very end of the log cannot be detected (nothing comes after them),
  and records written before 0.2.0 only have the plain hash chain.
- **A connected app sees what you ask it to do.** Delta cannot control what Notion, GitHub or any other app does
  with the requests it receives, and an app you trusted with read-only labels can mislabel a tool. A local MCP
  server is a program you chose to run with your Windows account's rights (minus Delta's secrets).
- **Environment scrubbing is by name.** A secret stored in a variable with an ordinary-looking name (not
  containing KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL, AUTH and similar) is passed on to commands Delta runs.

## Reporting a vulnerability

Report it privately via **GitHub Security Advisories** (Security tab → *Report a vulnerability*), not in a
public issue. Please include steps to reproduce. Security fixes go to the latest version on `main`.
