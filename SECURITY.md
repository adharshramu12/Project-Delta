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
  overwrites go to the Recycle Bin first), Autopilot (also sends messages).
- **Approvals** time out to *deny*.
- **Secrets** live in Windows Credential Manager, never in config, logs or chats.
- **Audit log** is hash-chained; `delta audit verify` detects tampering.
- **Prompt injection (enforced in code):** web pages, files and tool output are data and can never grant
  approval. Once a task has read outside content (a page, search result, file, chat message or the screen),
  every send, delete, code run or memory write in that task needs a human yes, **even in Autopilot and even
  for actions Delta has learned to trust**. So does desktop input that can submit or close something (Enter
  typed or pressed in an app, Alt+F4). Steps inside the open page (click, fill) stay free.
- **No read-then-leak through web addresses:** after a task has read outside content, opening a web address
  (`fetch_page`, `browser_open` in either browser mode, `open_target` with a URL) needs your yes when the site
  is new to the task. A site is not new when the task already opened it, when your own request names it, when
  it is in your `allowed_domains`, or when the exact address came back from `web_search` in that task. An
  address whose path, query or fragment carries a long encoded value (48+ base64/hex-like characters, or one
  query value over 200 characters) needs your yes even on a known site, and so does a `browser_search` whose
  text carries one or is over 200 characters.
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

## Remote control (Telegram): threat model

| Threat | Defence |
|---|---|
| A stranger finds or guesses the bot | Only paired Telegram accounts are served. Pairing needs a single-use 8-character code shown only on the PC's screen (10 minutes). The message must be exactly the code, and 5 wrong tries drop it until a new one is made on the PC. |
| **The owner's Telegram account or phone is stolen** | **Remote Guard**: camera, screenshot, sending files, closing apps, changing keys and Autopilot need the owner's **Delta PIN** (once per 15 minutes). |
| PIN guessing | **5 wrong PINs lock all remote control.** Only someone at the PC can unlock it; the phone never can. |
| PIN or key left in chat history | Delta deletes the message holding a PIN or API key right after reading it. |
| PIN stolen from the PC | Never stored: only a salted **scrypt** hash in Credential Manager, compared in constant time. |
| Something happens without the owner knowing | A **Windows notification** on the PC for every remote request and sensitive action; every unlock, failure, lock and action is in the audit log. |
| Owner suspects misuse | `/lock` from the phone locks all remote control at once; unlock only on the PC. |
| Bot token leaked | Stored in Credential Manager; replace it from the PC. Pairing still stops a token holder from controlling the PC. |

**Recommended for every user:** turn on Telegram's **Two-Step Verification**, and set a **Delta PIN**
(Settings → Remote security). Delta reminds you when pairing.

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

## Reporting a vulnerability

Report it privately via **GitHub Security Advisories** (Security tab → *Report a vulnerability*), not in a
public issue. Please include steps to reproduce. Security fixes go to the latest version on `main`.
