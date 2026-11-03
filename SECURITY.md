# Security model

Delta controls a real computer, so its safety rules are part of the design, not settings.

## Safety floor (no setting can unlock it)

- **Denied always:** OS/security settings, disk formatting, user accounts, stored credentials, firewall/antivirus
  changes and money movement. Protected folders (Windows, credential stores, SSH/AWS/GPG keys, Delta's own data).
- **Asked always:** changes that cannot be undone, such as a shell deleting files or Shift+Delete.
- **Permission levels** decide the rest: Careful, Balanced, Trusted (undoable work without asking: deletes and
  overwrites go to the Recycle Bin first), Autopilot (also sends messages).
- **Approvals** time out to *deny*.
- **Secrets** live in Windows Credential Manager, never in config, logs or chats.
- **Audit log** is hash-chained; `delta audit verify` detects tampering.
- **Prompt injection (enforced in code):** web pages, files and tool output are data and can never grant
  approval. Once a task has read outside content (a page, search result, file, chat message or the screen),
  every send, delete, code run or memory write in that task needs a human yes, **even in Autopilot and even
  for actions Delta has learned to trust**. Steps inside the open page (click, fill) stay free.
- **Encryption at rest:** memories, chat titles and messages are encrypted with **Windows DPAPI**, tied to
  your Windows account. Another account, a copied disk or a stolen database file cannot read them.
- **The dashboard API** listens on this PC only (127.0.0.1) and needs a per-install secret token.
- **Your own browser:** Delta never types into password or payment fields; it asks you to do that.
- **Emergency contacts** are stored encrypted (DPAPI). During a help request, reaching anyone needs your yes
  (even in Autopilot) unless your own message clearly says to ("message Kushi now"). Delta is not an emergency service: for anything life-threatening it tells you to call 112.
- **AI apps you sign in to** (Claude Code, Codex, Cline) are used only as text brains: their own tools are
  switched off or read-only, so Delta's rules apply to every action whichever AI is thinking.
- **Your data stays on your PC** (`%LOCALAPPDATA%\Delta`): memory, chats, skills, audit log. Shutdown stops
  every Delta process; nothing keeps listening.

## Remote control (Telegram): threat model

| Threat | Defence |
|---|---|
| A stranger finds or guesses the bot | Only paired Telegram accounts are served. Pairing needs a single-use 6-digit code shown only on the PC's screen (10 minutes). |
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
  Delta's answers or in-page steps, so check important results.
- **Cloud AI providers** see the requests sent to them. Use a local model (LM Studio, Ollama) for private work.

## Reporting a vulnerability

Report it privately via **GitHub Security Advisories** (Security tab → *Report a vulnerability*), not in a
public issue. Please include steps to reproduce. Security fixes go to the latest version on `main`.
