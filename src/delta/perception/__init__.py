"""Perception: how Delta sees a screen when reading it as text is not enough.

    marks.py    set-of-marks: numbered boxes over clickable elements, a compact legend, and the book of
                marks a later click_mark resolves to an element handle (a page ref or a UI Automation element)
    images.py   downscaling, JPEG quality and an image token estimate, tuned for tokens
    sources.py  where marks come from: the page structure of Delta's own browser, the Windows UI Automation
                tree of a desktop window, or Windows OCR when an app has no accessibility tree
    ocr.py      Windows' built-in OCR (Windows.Media.Ocr) through Windows PowerShell; a no-op elsewhere
    cache.py    a hash of each view sent, so an unchanged screen or window is not sent again
    tools.py    look_with_marks, click_mark, read_screen_text, click_text

Pictures stay on this PC, except that a marked picture goes to the vision model the user chose.
"""
