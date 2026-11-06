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
- **Audit log** is hash-chained; `delta audit verify` detects tampering.
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
  your Windows account. Another account, a copied disk or a stolen database file cannot read them.
- **The dashboard API** listens on this PC only (127.0.0.1) and needs a per-install secret token.
- **Your own browser:** Delta never types into password or payment fields; it asks you to do that.
- **Emergency contacts** are stored encrypted (DPAPI). During a help request, reaching anyone needs your yes
  (even in Autopilot) unless your own message clearly says to ("message Ravi now"). Delta is not an emergency service: for anything life-threatening it tells you to call 112.
- **AI apps you sign in to** (Claude Code, Codex, Cline) are used only as text brains: their own tools are
  switched off or read-only, so Delta's rules apply to every action whichever AI is thinking.
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

## Remote control (Telegram, Slack): threat model

| Threat | Defence |
|---|---|
| A stranger finds or guesses the bot | Only paired Telegram accounts are served. Pairing needs a single-use 8-character code shown only on the PC's screen (10 minutes). The message must be exactly the code, and 5 wrong tries drop it until a new one is made on the PC. |
| **The owner's Telegram account or phone is stolen** | **Remote Guard** (see below): seeing the screen or webcam, reading files, windows, messages or the clipboard, sending files, running commands or code, closing apps, changing keys, turning on Autopilot, and approving deletes or sends from the phone all need the owner's **Delta PIN** (once per 15 minutes). |
| PIN guessing | **5 wrong PINs lock all remote control.** Only someone at the PC can unlock it; the phone never can. |
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

### What is *not* protected (honestly)

- **Telegram bot chats are not end-to-end encrypted.** Messages travel encrypted, but Telegram's servers can
  read them. Delta minimises what passes through (keys and PINs are deleted at once; pictures are deleted
  from the PC after sending). For end-to-end encryption, a Matrix channel is planned.
- **Malware already running as the same Windows user** can ask Windows to decrypt that user's DPAPI data,
  like any other app the user runs. DPAPI protects against other accounts and stolen files, not that.
- **Prompt-injection containment** guards risky actions, not reading: an injected page can still mislead
  Delta's answers or in-page steps, so check important results. In-page steps include following a link by
  clicking it and filling a search box, so a page can still move the browser to another page with a short
  piece of data in its address; long encoded values and new sites opened by address are what is guarded.
- **Clicks in a `do_actions` batch** are judged by what is on screen before the batch starts, so a click on a
  menu item the batch itself opens is judged by what was under that point before the menu appeared.
- **Cloud AI providers** see the requests sent to them. Use a local model (LM Studio, Ollama) for private work.
- **Scheduled task text is stored as plain text** in `schedules.db` (unlike memory, which is encrypted), so do
  not put secrets in a schedule. It sits in Delta's data folder, readable by your Windows account.
- **The step log is plain text** in `tasks.db` (redacted as above, not encrypted like memory). File names, page
  addresses and short results of your tasks are in it for 30 days by default (`engine.retention_days`).
- **If you raise the permission level while a scheduled task is running**, that run follows your new level
  from then on: a change you make yourself is not treated as the schedule raising it.

## Reporting a vulnerability

Report it privately via **GitHub Security Advisories** (Security tab → *Report a vulnerability*), not in a
public issue. Please include steps to reproduce. Security fixes go to the latest version on `main`.
