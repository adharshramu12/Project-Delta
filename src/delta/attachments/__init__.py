"""Attachments: pictures and documents the user sends in the dashboard chat or on Telegram.

    kinds.py    what a file is (by its first bytes, not only its name), safe names, the limits in plain words
    store.py    files on disk in Delta's protected data folder, one folder per chat, removed after retention_days
    pdf.py      a small PDF text reader (standard library only): text layer per page, object streams, ToUnicode
    ooxml.py    Word (.docx) and PowerPoint (.pptx) with zipfile and a hardened expat parser: headings, slides
                with titles, speaker notes and tables; never runs macros or opens embedded objects
    images.py   pictures: safe opening, downscaling and re-encoding for a vision model
    winpdf.py   renders chosen PDF pages to pictures with Windows' own PDF engine (Windows.Data.Pdf), for
                scanned pages; a no-op elsewhere
    extract.py  one structured extract per file (parts with page or slide numbers), cached by file hash
    service.py  what goes to the AI: a compact extract with the request, a picture only when it matters,
                and the attachment counters in the token meter
    tools.py    read_attachment (more pages on demand) and look_at_attachment (one picture to a vision model)

Everything inside a file is outside content: the task is marked as having read outside content (ExfilGuard and
the taint rule apply), memories from it are labelled as from an attachment, and the AI is told that
instructions inside a file are data. Nothing here needs a new dependency: Pillow was already one.
"""
