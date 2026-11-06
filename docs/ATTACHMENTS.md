# Attachments: pictures and documents in the chat and on Telegram

You can send Delta a picture or a document with your request, in the dashboard chat or on Telegram, and ask about
it: "summarise this", "what does slide 4 say?", "what is wrong in this screenshot?". Delta reads the file on your
PC first and sends the AI only what it needs, so a long PDF does not cost thousands of tokens on every call.

## How to send a file

- **Dashboard:** press the paper clip next to the message box, drag files onto the chat, or paste a picture
  (Ctrl+V). Each file shows as a chip that says what Delta found ("PDF, 20 pages", "Picture, text only"). Remove
  one with its x before you send. You can send files with no message; Delta then summarises them.
- **Telegram:** send a photo or a file, with a caption that says what you want. Photos sent together as an album
  become one request. Only paired phones can send files, nothing is read while remote control is locked, and the
  PIN rules apply to whatever the task does next.

## Formats

| Kind | Files | What Delta reads |
|---|---|---|
| Pictures | JPG, PNG, WebP, GIF, BMP | Text with Windows OCR when the PC has it; the picture itself goes to a vision model only when it matters |
| PDF | .pdf | The text layer, page by page, with page numbers. Scanned pages are rendered and read with Windows OCR |
| Word | .docx | Headings and paragraphs in order, tables as rows, split into numbered parts |
| PowerPoint | .pptx | Each slide in order: title, text, tables, picture alt text and speaker notes |
| Text | .txt, .md, .csv (also .tsv, .log, .json) | The text, split into numbered parts |

The kind is decided by the file's first bytes, not only its name, so a renamed file is still caught. Files Delta
cannot read get a plain answer that says what to do instead: old .doc and .ppt (save as .docx or .pptx), Excel
(save the sheet as .csv), iPhone HEIC photos (send as a normal photo or JPG), and zip files (not opened, for
safety).

## Limits

| Limit | Default | Setting |
|---|---|---|
| One file | 20 MB (the most a Telegram bot can download) | `attachments.max_mb` |
| Files per message | 5 | `attachments.max_files_per_message` |
| Pages, slides or Word parts read | 300 | `attachments.max_pages` |
| Kept on disk | 30 days, and removed with their chat | `attachments.retention_days` (0 keeps them) |

## How Delta keeps the token cost down

1. **Text first, on your PC.** PDFs, Word, PowerPoint and text files are read locally with Delta's own readers.
   Pictures are read with the OCR built into Windows 10 and 11 when it is available.
2. **A short extract goes with the request**, not the whole file: an outline with page or slide numbers, then the
   first pages up to `prompt_chars` (6,000 characters, about 1,500 tokens, for all files together), cut with a
   clear marker.
3. **The AI asks for more only when it needs it**, with `read_attachment(id, pages)` ("3", "2-5", "last") or
   `read_attachment(id, find)` to search the file. Each answer is at most `read_chars` (8,000 characters).
4. **A picture goes to a model only when the picture itself matters**: a photo, a chart, a screenshot with little
   text, or a question about how something looks. One picture per call, made no bigger than 1024 px on its longest
   side, re-encoded as JPEG, and sent in low detail where the provider supports it (OpenAI) for plain photos.
   `look_at_attachment` does this on demand, also for a rendered PDF page.
5. **Cached by file hash.** Asking again about the same file, in any chat, costs no reading, and the same
   question about the same picture costs no second model call.

Every answer's usage line shows the attachment tokens (for example "2 file(s): ~1.2k tokens, 1 picture(s) seen"),
the task progress shows "Read 2 file(s) on this PC: ~1.2k tokens instead of 15.8k", and the chat's usage box adds
up the attachment tokens for the conversation.

### What it saves

These are estimates (about 4 characters per token for text; each provider's own rules for pictures), not
measurements of a particular bill.

| File | Sent whole | What Delta sends |
|---|---|---|
| 20-page text PDF | about 15,800 tokens | about 1,100 to 1,500 tokens (extract), plus any pages the AI asks for |
| 4000 x 3000 phone photo, sent as is | about 765 to 1,600 tokens, depending on the provider | 0 tokens when Windows OCR answers the question |
| The same photo when the picture matters | | about 765 tokens at 1024 px, or 85 tokens in low detail on OpenAI |

## Pictures and which AI looks at them

Settings, Attachments, **Pictures**:

- **Any AI I have that can see pictures** (`vision: auto`, the default): the picture goes to a model on your normal
  route that supports pictures.
- **Off** (`vision: off`): no picture is ever sent; Delta uses the text in it (OCR) and says so.
- **Only one provider, for pictures** (`vision: <provider name>`): pictures go to that provider. With **Model for
  pictures** (`vision_model`) set, Delta builds a separate route with only that provider and model, used for
  attached pictures only. This is opt-in: your main AI, your other routes and AI apps such as Claude Code or Codex
  do not change.

If no AI you have can see pictures, Delta answers from the text in the picture and tells you plainly that it did
not see the picture itself, and how to turn it on.

## Safety

- **What is in a file is outside content.** The task is marked as having read outside content, so later sends,
  deletes, code and memory writes ask you first (the taint rule and ExfilGuard), exactly as after reading a web
  page. The AI is told that instructions inside a file are data, not orders. A scripted eval checks that even a model
  fooled by text inside a Word file cannot act on it without your OK.
- **Memory provenance.** A memory learned from a file is labelled as coming from "a file you attached", not from
  you, and is kept as data only.
- **Hardened readers, no new dependencies.** The PDF reader uses only the standard library, maps the file instead
  of loading it, caps every decompressed stream, object count and nesting depth (a zip bomb stops at the cap), and
  never runs JavaScript, actions or embedded files. Word and PowerPoint are opened with `zipfile` and a hardened
  expat parser: no DOCTYPE, so no entity expansion and no external entities; a cap on entries and on inflated
  size; nothing extracted to disk. Macros, embedded objects and ActiveX are never opened or run; the extract only
  says they were there. Pictures are checked for an absurd pixel count before decoding.
- **Storage.** Uploads are streamed to disk in small chunks while the size limit is counted, stored under a random
  id (the original name is only shown, never used as a path), inside Delta's data folder, which Delta's own file
  tools can never write.
- **Dashboard.** File names and extracts are shown as text only (never as HTML), the strict Content Security
  Policy is unchanged, and uploads and previews need the dashboard's access token.

## Settings

```yaml
attachments:
  enabled: true
  max_mb: 20
  max_pages: 300
  max_files_per_message: 5
  retention_days: 30
  prompt_chars: 6000          # extract sent with the request (about 1,500 tokens)
  read_chars: 8000            # one read_attachment answer at most
  cache_mb: 50                # extracts kept by file hash
  vision: auto                # auto | off | the name of one of your providers, for pictures only
  vision_model: ""            # with a named provider: its model for pictures
  image_max_side: 1024
  image_detail: auto          # auto | low | high
  ocr: auto                   # Windows OCR for pictures and scanned pages | off
```

## Known tradeoffs

- **Encrypted PDFs** cannot be read by Delta's own reader. Delta falls back to rendering the needed pages with
  Windows' PDF engine and reading them with OCR, which works only for PDFs Windows can open, is slower and can
  miss text.
- **Pictures inside Word and PowerPoint files** are described by their alt text only; the pictures themselves are
  not sent to a vision model.
- **Attachment files are stored unencrypted** in Delta's protected data folder (unlike memory and chats, which are
  encrypted with DPAPI). They are removed after `retention_days` and with their chat.
- **Resumed tasks do not restore attachments.** A task resumed after a crash or power cut runs without the files
  it was sent with; send them again if the task needs them.
- Old Office formats (.doc, .ppt), Excel files and HEIC photos are not read (see Formats).
- Token numbers in the usage line are estimates where the provider does not report its own count.

What has not been checked on real hardware is listed in [NOT_VERIFIED.md](NOT_VERIFIED.md#attachments).
