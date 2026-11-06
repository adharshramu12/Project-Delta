"""A small PDF text reader using only the standard library (zlib, mmap, re).

Delta had no PDF library, and adding one to every install for this was not worth it. This reads the text layer
of the PDFs people actually send (Word and Google Docs exports, browser "Save as PDF", most reports and slides):

- objects anywhere in the file, including compressed object streams (PDF 1.5+), so a broken or missing
  cross-reference table does not matter; a later copy of an object (an incremental update) wins
- the page tree in reading order, with inherited resources
- text operators (Tj, TJ, ', ") with line breaks from Td / TD / T* / Tm, form XObjects one level deep
- fonts with ToUnicode maps (one and two byte codes), WinAnsi / MacRoman / Standard encodings and Differences

It never runs anything (no JavaScript, no actions, no embedded files) and is bounded everywhere: the file is mapped,
not loaded; each decompressed stream and the whole document have byte caps (a zip bomb stops at the cap); object
and page counts are capped; nesting is limited. A page with images and no text is reported as scanned, and an
encrypted PDF is reported as such, so the caller can render and OCR those pages instead (winpdf.py).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import mmap
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_STREAM = 16 * 1024 * 1024     # one decompressed stream
MAX_TOTAL = 96 * 1024 * 1024      # all decompressed bytes for one document
MAX_OBJECTS = 400_000
MAX_DEPTH = 40                     # nesting of arrays / dictionaries
MAX_TREE = 30                      # page tree depth
MAX_PAGE_CHARS = 200_000


class PdfError(ValueError):
    pass


class Name(str):
    """/Name"""


class PStr(bytes):
    """(literal) or <hex> string"""


@dataclass(frozen=True)
class Ref:
    num: int
    gen: int = 0


@dataclass
class Stream:
    dict: dict[str, Any]
    raw: bytes


@dataclass
class PdfPage:
    number: int
    text: str
    images: int = 0

    @property
    def scanned(self) -> bool:
        return len(self.text.strip()) < 15 and self.images > 0


@dataclass
class PdfDoc:
    page_count: int
    pages: list[PdfPage] = field(default_factory=list)
    encrypted: bool = False
    title: str = ""
    truncated: bool = False  # more pages than max_pages
    problems: list[str] = field(default_factory=list)


# ---- lexing -----------------------------------------------------------------------------------------------------
WS = b" \t\r\n\x0c\x00"
DELIM = b"()<>[]{}/%"
_TOKEN = re.compile(rb"""
    (?P<ws>[ \t\r\n\x0c\x00]+|%[^\r\n]*)
  | (?P<dopen><<) | (?P<dclose>>>)
  | (?P<hex><[0-9A-Fa-f \t\r\n\x0c]*>)
  | (?P<lit>\()
  | (?P<aopen>\[) | (?P<aclose>\])
  | (?P<name>/[^ \t\r\n\x0c\x00()<>\[\]{}/%]*)
  | (?P<num>[+-]?(?:\d+\.?\d*|\.\d+))(?![^ \t\r\n\x0c\x00()<>\[\]{}/%])
  | (?P<kw>[^ \t\r\n\x0c\x00()<>\[\]{}/%]+)
  | (?P<other>.)
""", re.X | re.S)
_ESC = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
        ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


def _literal(data: bytes, pos: int) -> tuple[PStr, int]:
    """A (literal string) starting just after '('; returns it and the position after ')'."""
    out = bytearray()
    depth, i, n = 1, pos, len(data)
    while i < n:
        c = data[i]
        if c == 0x5C and i + 1 < n:  # backslash
            nxt = data[i + 1]
            if nxt in _ESC:
                out += _ESC[nxt]
                i += 2
            elif 0x30 <= nxt <= 0x37:
                j = i + 1
                while j < min(i + 4, n) and 0x30 <= data[j] <= 0x37:
                    j += 1
                out.append(int(data[i + 1:j], 8) & 0xFF)
                i = j
            elif nxt in (0x0D, 0x0A):  # line continuation
                i += 2
                if nxt == 0x0D and i < n and data[i] == 0x0A:
                    i += 1
            else:
                out.append(nxt)
                i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return PStr(bytes(out)), i + 1
        out.append(c)
        i += 1
    return PStr(bytes(out)), n


def _hex(tok: bytes) -> PStr:
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", tok)
    if len(digits) % 2:
        digits += b"0"
    return PStr(binascii.unhexlify(digits))


def _name(tok: bytes) -> Name:
    raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), tok[1:])
    return Name(raw.decode("latin-1"))


def tokens(data: bytes, pos: int = 0, end: int | None = None) -> Iterator[tuple[str, Any, int]]:
    """(kind, value, position after it) for each token from `pos`."""
    end = len(data) if end is None else end
    while pos < end:
        m = _TOKEN.match(data, pos, end)
        if not m:
            return
        kind = m.lastgroup or "other"
        pos = m.end()
        if kind == "ws":
            continue
        tok = m.group(0)
        if kind == "lit":
            value, pos = _literal(data, pos)
            yield "str", value, pos
        elif kind == "hex":
            yield "str", _hex(tok), pos
        elif kind == "name":
            yield "name", _name(tok), pos
        elif kind == "num":
            yield "num", (float(tok) if b"." in tok else int(tok)), pos
        else:
            yield kind, tok, pos


def parse_value(data: bytes, pos: int, depth: int = 0) -> tuple[Any, int]:
    """One PDF object (dict, array, ref, string, name, number, bool, null) from `pos`."""
    if depth > MAX_DEPTH:
        raise PdfError("objects nested too deeply")
    it = tokens(data, pos)
    for kind, value, after in it:
        if kind == "dopen":
            out: dict[str, Any] = {}
            p = after
            while True:
                k, p2 = _next(data, p)
                if k is None or k == ("dclose",):
                    return out, p2
                if not isinstance(k, Name):
                    p = p2
                    continue
                v, p = parse_value(data, p2, depth + 1)
                out[str(k)] = v
        if kind == "aopen":
            arr: list[Any] = []
            p = after
            while True:
                peek = _TOKEN.match(data, p)
                while peek and peek.lastgroup == "ws":
                    p = peek.end()
                    peek = _TOKEN.match(data, p)
                if not peek or peek.lastgroup == "aclose":
                    return arr, (peek.end() if peek else len(data))
                v, p = parse_value(data, p, depth + 1)
                arr.append(v)
        if kind == "num":
            # "12 0 R" is a reference
            m = re.compile(rb"[ \t\r\n\x0c\x00]+(\d+)[ \t\r\n\x0c\x00]+R(?![A-Za-z])").match(data, after)
            if isinstance(value, int) and m:
                return Ref(value, int(m.group(1))), m.end()
            return value, after
        if kind in ("str", "name"):
            return value, after
        if kind == "kw":
            if value == b"true":
                return True, after
            if value == b"false":
                return False, after
            return None, after
        if kind in ("dclose", "aclose"):
            return ("dclose",) if kind == "dclose" else None, after
        return None, after
    return None, len(data)


def _next(data: bytes, pos: int) -> tuple[Any, int]:
    m = _TOKEN.match(data, pos)
    while m and m.lastgroup == "ws":
        pos = m.end()
        m = _TOKEN.match(data, pos)
    if not m:
        return None, len(data)
    if m.lastgroup == "dclose":
        return ("dclose",), m.end()
    return parse_value(data, pos)


# ---- filters ------------------------------------------------------------------------------------------------------
class _Budget:
    def __init__(self, total: int = MAX_TOTAL) -> None:
        self.left = total

    def take(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise PdfError("the PDF expands to far more data than it should (it may be a zip bomb)")


def _inflate(raw: bytes, budget: _Budget) -> bytes:
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        d = zlib.decompressobj(wbits)
        try:
            out = d.decompress(raw, MAX_STREAM)
        except zlib.error:
            continue
        if d.unconsumed_tail:
            raise PdfError("a part of the PDF expands past the size limit (it may be a zip bomb)")
        budget.take(len(out))
        return out
    return b""  # damaged stream: no text from it


def decode_stream(s: Stream, budget: _Budget) -> bytes:
    filters = s.dict.get("Filter")
    filters = filters if isinstance(filters, list) else [filters] if filters else []
    data = s.raw
    for f in filters:
        f = str(f)
        if f in ("FlateDecode", "Fl"):
            data = _inflate(data, budget)
        elif f in ("ASCIIHexDecode", "AHx"):
            data = _hex(data.split(b">")[0])
        elif f in ("ASCII85Decode", "A85"):
            body = data.strip()
            body = body[2:] if body.startswith(b"<~") else body
            try:
                data = base64.a85decode(body.split(b"~>")[0], adobe=False, ignorechars=WS)
            except ValueError:
                return b""
        else:
            return b""  # images (DCT, JBIG2, CCITT) and rare filters: no text here
        if len(data) > MAX_STREAM:
            raise PdfError("a part of the PDF is too large")
    return data


# ---- the document -----------------------------------------------------------------------------------------------
_OBJ = re.compile(rb"(?<![0-9])(\d{1,10})[ \t\r\n\x0c\x00]+(\d{1,5})[ \t\r\n\x0c\x00]+obj\b")


class Reader:
    def __init__(self, data: Any) -> None:
        self.data = data  # bytes or mmap
        self.budget = _Budget()
        self.offsets: dict[int, int] = {}
        self.in_stream: dict[int, tuple[int, int]] = {}  # obj -> (object stream number, index)
        self._cache: dict[int, Any] = {}
        self._objstm: dict[int, tuple[bytes, list[tuple[int, int]], int]] = {}
        for m in _OBJ.finditer(data):
            self.offsets[int(m.group(1))] = m.end()  # a later copy (incremental update) wins
            if len(self.offsets) > MAX_OBJECTS:
                raise PdfError("the PDF has too many objects")
        self._index_object_streams()

    def _index_object_streams(self) -> None:
        for num, off in list(self.offsets.items()):
            peek = bytes(self.data[off:off + 300])
            if b"/ObjStm" not in peek:
                continue
            obj = self.get(num)
            if not isinstance(obj, Stream) or obj.dict.get("Type") != "ObjStm":
                continue
            try:
                body = decode_stream(obj, self.budget)
                n, first = int(obj.dict.get("N", 0)), int(obj.dict.get("First", 0))
                nums = [int(x) for x in body[:first].split()[: 2 * n]]
            except (ValueError, TypeError):
                continue
            pairs = list(zip(nums[0::2], nums[1::2], strict=False))
            self._objstm[num] = (body, pairs, first)
            for idx, (onum, _) in enumerate(pairs):
                if onum not in self.offsets:  # a plain object of the same number is newer
                    self.in_stream[onum] = (num, idx)

    def get(self, num: int) -> Any:
        if num in self._cache:
            return self._cache[num]
        self._cache[num] = None  # a reference loop resolves to nothing
        value: Any = None
        if num in self.offsets:
            value = self._parse_at(self.offsets[num])
        elif num in self.in_stream:
            stm, idx = self.in_stream[num]
            body, pairs, first = self._objstm.get(stm, (b"", [], 0))
            if idx < len(pairs):
                try:
                    value, _ = parse_value(body, first + pairs[idx][1])
                except PdfError:
                    value = None
        if len(self._cache) > 50_000:
            self._cache.clear()
        self._cache[num] = value
        return value

    def _parse_at(self, off: int) -> Any:
        window = bytes(self.data[off:off + 65536])
        try:
            value, after = parse_value(window, 0)
        except PdfError:
            return None
        if isinstance(value, dict):
            m = re.compile(rb"[ \t\r\n\x0c\x00]*stream(\r\n|\n|\r)").match(window, after)
            if m:
                start = off + m.end()
                length = self.resolve(value.get("Length"))
                if isinstance(length, int) and 0 <= length <= MAX_TOTAL:
                    tail = bytes(self.data[start + length:start + length + 40])
                    if b"endstream" in tail:
                        return Stream(value, bytes(self.data[start:start + length]))
                end = self.data.find(b"endstream", start)
                if end < 0:
                    return None
                raw = bytes(self.data[start:end]).rstrip(b"\r\n")
                return Stream(value, raw)
        return value

    def resolve(self, v: Any, depth: int = 0) -> Any:
        while isinstance(v, Ref) and depth < 20:
            v = self.get(v.num)
            depth += 1
        return v

    def trailer_ref(self, key: str) -> Ref | None:
        found = None
        pattern = rb"/" + key.encode() + rb"[ \t\r\n\x0c\x00]+(\d+)[ \t\r\n\x0c\x00]+(\d+)[ \t\r\n\x0c\x00]+R"
        for m in re.finditer(pattern, self.data):
            found = Ref(int(m.group(1)), int(m.group(2)))
        return found

    def encrypted(self) -> bool:
        return bool(re.search(rb"/Encrypt[ \t\r\n\x0c\x00]*(?:\d+[ \t\r\n\x0c\x00]+\d+[ \t\r\n\x0c\x00]+R|<<)",
                              self.data))

    def catalog(self) -> dict[str, Any] | None:
        ref = self.trailer_ref("Root")
        cat = self.resolve(ref) if ref else None
        if isinstance(cat, dict) and "Pages" in cat:
            return cat
        for num in list(self.offsets)[:MAX_OBJECTS]:
            obj = self.get(num)
            if isinstance(obj, dict) and obj.get("Type") == "Catalog":
                return obj
        return None

    def page_dicts(self, limit: int) -> tuple[list[dict[str, Any]], int]:
        """Page dictionaries in reading order (resources inherited from parents), and the declared page count."""
        cat = self.catalog()
        root = self.resolve(cat.get("Pages")) if cat else None
        out: list[dict[str, Any]] = []
        seen: set[int] = set()

        def walk(node: Any, inherited: dict[str, Any], depth: int) -> None:
            if depth > MAX_TREE or len(out) >= limit or not isinstance(node, dict):
                return
            inh = dict(inherited)
            for key in ("Resources", "MediaBox", "Rotate"):
                if key in node:
                    inh[key] = node[key]
            kids = self.resolve(node.get("Kids"))
            if node.get("Type") == "Page" or (kids is None and "Contents" in node):
                out.append({**inh, **node})
                return
            for kid in kids if isinstance(kids, list) else []:
                if isinstance(kid, Ref):
                    if kid.num in seen:
                        continue
                    seen.add(kid.num)
                walk(self.resolve(kid), inh, depth + 1)

        walk(root, {}, 0)
        declared = self.resolve(root.get("Count")) if isinstance(root, dict) else None
        if not out:  # no usable page tree: every Page object in file order
            for num in sorted(self.offsets, key=lambda n: self.offsets[n]):
                obj = self.get(num)
                if isinstance(obj, dict) and obj.get("Type") == "Page":
                    out.append(obj)
                    if len(out) >= limit:
                        break
        return out, int(declared) if isinstance(declared, int) else len(out)

    def info_title(self) -> str:
        info = self.resolve(self.trailer_ref("Info"))
        title = info.get("Title") if isinstance(info, dict) else None
        return _pdf_text(title).strip()[:200] if isinstance(title, bytes) else ""


def _pdf_text(b: bytes) -> str:
    """A text string from the document's own data (title): UTF-16 with a byte-order mark, else PDFDocEncoding."""
    if b.startswith(b"\xfe\xff"):
        return b[2:].decode("utf-16-be", "replace")
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace")
    return b.decode("latin-1")


# ---- fonts ------------------------------------------------------------------------------------------------------
_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "'", "quoteright": "\u2019", "quoteleft": "\u2018", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "minus": "-", "period": ".",
    "slash": "/", "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">", "question": "?",
    "at": "@", "bracketleft": "[", "backslash": "\\", "bracketright": "]", "underscore": "_", "braceleft": "{",
    "bar": "|", "braceright": "}", "endash": "-", "emdash": "-", "bullet": "\u2022", "fi": "fi", "fl": "fl",
    "ff": "ff", "ffi": "ffi", "ffl": "ffl", "quotedblleft": "\u201c", "quotedblright": "\u201d",
    "ellipsis": "...", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "nbspace": " ", "rupee": "\u20b9", "Euro": "\u20ac",
}


def _glyph(name: str) -> str:
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    m = re.fullmatch(r"(?:uni|u)([0-9A-Fa-f]{4,6})", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return ""
    return ""


class Font:
    def __init__(self, reader: Reader, d: Any) -> None:
        d = reader.resolve(d)
        d = d if isinstance(d, dict) else {}
        self.two_byte = d.get("Subtype") == "Type0"
        self.cmap: dict[int, str] = {}
        self.code_lens: list[int] = []
        self.base = "cp1252"
        self.diff: dict[int, str] = {}
        tu = reader.resolve(d.get("ToUnicode"))
        if isinstance(tu, Stream):
            with contextlib.suppress(PdfError):
                self._read_cmap(decode_stream(tu, reader.budget))
        enc = reader.resolve(d.get("Encoding"))
        if isinstance(enc, Name):
            self.base = {"MacRomanEncoding": "mac_roman"}.get(str(enc), "cp1252")
            if str(enc).startswith("Identity"):
                self.two_byte = True
        elif isinstance(enc, dict):
            base = enc.get("BaseEncoding")
            if base == "MacRomanEncoding":
                self.base = "mac_roman"
            code = 0
            for item in reader.resolve(enc.get("Differences")) or []:
                if isinstance(item, int):
                    code = item
                elif isinstance(item, Name):
                    if glyph := _glyph(str(item)):  # an unknown name (a65, g12, .notdef): the base encoding
                        self.diff[code] = glyph
                    code += 1
        if not self.code_lens:
            self.code_lens = [2] if self.two_byte else [1]

    def _read_cmap(self, text: bytes) -> None:
        lens: set[int] = set()
        for block in re.findall(rb"begincodespacerange(.*?)endcodespacerange", text, re.S):
            for lo in re.findall(rb"<([0-9A-Fa-f]+)>\s*<[0-9A-Fa-f]+>", block):
                lens.add(max(1, len(lo) // 2))
        for block in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.S):
            for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
                self.cmap[int(src, 16)] = _utf16(dst)
                lens.add(max(1, len(src) // 2))
                if len(self.cmap) > 70_000:
                    return
        for block in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.S):
            ranges = re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block)
            for lo, hi, rest in ranges:
                a, b = int(lo, 16), int(hi, 16)
                if b < a or b - a > 65535:
                    continue
                lens.add(max(1, len(lo) // 2))
                if rest.startswith(b"["):
                    for i, dst in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", rest)):
                        self.cmap[a + i] = _utf16(dst)
                else:
                    start = _utf16(rest[1:-1])
                    if not start:
                        continue
                    head, last = start[:-1], ord(start[-1])
                    for i in range(b - a + 1):
                        try:
                            self.cmap[a + i] = head + chr(last + i)
                        except (ValueError, OverflowError):
                            break
                if len(self.cmap) > 70_000:
                    return
        if lens:
            self.code_lens = sorted(lens, reverse=True)

    def decode(self, s: bytes) -> str:
        if self.cmap:
            out, i = [], 0
            while i < len(s):
                for n in self.code_lens:
                    code = int.from_bytes(s[i:i + n], "big")
                    if i + n <= len(s) and code in self.cmap:
                        out.append(self.cmap[code])
                        i += n
                        break
                else:
                    n = self.code_lens[-1]
                    if not self.two_byte and n == 1:
                        out.append(self._simple(s[i]))
                    i += n
            return "".join(out)
        if self.two_byte:
            return ""  # CID font without a ToUnicode map: no reliable text (the page reads as scanned)
        return "".join(self._simple(c) for c in s)

    def _simple(self, c: int) -> str:
        if c in self.diff:
            return self.diff[c]
        return bytes([c]).decode(self.base, "replace") if c >= 32 else ""


def _utf16(hexdigits: bytes) -> str:
    raw = binascii.unhexlify(hexdigits + (b"0" if len(hexdigits) % 2 else b""))
    if len(raw) % 2:
        raw += b"\x00"
    return raw.decode("utf-16-be", "replace")


# ---- page text ----------------------------------------------------------------------------------------------------
_TEXT_OPS = {b"Tj", b"TJ", b"'", b'"'}


def _content(reader: Reader, page: dict[str, Any]) -> bytes:
    contents = reader.resolve(page.get("Contents"))
    parts = contents if isinstance(contents, list) else [contents]
    out = []
    for p in parts:
        s = reader.resolve(p)
        if isinstance(s, Stream):
            out.append(decode_stream(s, reader.budget))
    return b"\n".join(out)


def _fonts(reader: Reader, resources: Any) -> dict[str, Font]:
    res = reader.resolve(resources)
    fonts = reader.resolve(res.get("Font")) if isinstance(res, dict) else None
    return {k: Font(reader, v) for k, v in fonts.items()} if isinstance(fonts, dict) else {}


def _xobjects(reader: Reader, resources: Any) -> dict[str, Any]:
    res = reader.resolve(resources)
    xo = reader.resolve(res.get("XObject")) if isinstance(res, dict) else None
    return xo if isinstance(xo, dict) else {}


def page_text(reader: Reader, data: bytes, resources: Any, depth: int = 0) -> tuple[str, int]:
    """The text of one content stream and how many images it draws."""
    fonts = _fonts(reader, resources)
    xobjects = _xobjects(reader, resources)
    font: Font | None = None
    out: list[str] = []
    images = 0
    operands: list[Any] = []
    y: float | None = None
    size = 0

    def emit(text: str) -> None:
        nonlocal size
        if text:
            out.append(text)
            size += len(text)

    pos, n = 0, len(data)
    while pos < n and size < MAX_PAGE_CHARS:
        m = _TOKEN.match(data, pos)
        if not m:
            break
        kind = m.lastgroup
        pos = m.end()
        if kind == "ws":
            continue
        tok = m.group(0)
        if kind in ("lit", "hex", "name", "num", "aopen", "dopen"):
            try:
                value, pos = parse_value(data, m.start())
            except PdfError:
                break
            operands.append(value)
            if len(operands) > 64:
                operands = operands[-64:]
            continue
        if kind != "kw":
            operands.clear()
            continue
        op = tok
        if op == b"BI":  # inline image: skip its binary data
            end = re.compile(rb"[ \t\r\n\x0c\x00]EI(?=[ \t\r\n\x0c\x00]|$)").search(data, pos)
            pos = end.end() if end else n
            images += 1
            operands.clear()
            continue
        if op == b"Tf" and operands:
            name = next((o for o in operands if isinstance(o, Name)), None)
            font = fonts.get(str(name)) if name is not None else None
        elif op in _TEXT_OPS:
            dec = font.decode if font else (lambda b: b.decode("cp1252", "replace"))
            if op in (b"'", b'"'):
                emit("\n")
            for o in operands:
                if isinstance(o, PStr):
                    emit(dec(o))
                elif isinstance(o, list):
                    for item in o:
                        if isinstance(item, PStr):
                            emit(dec(item))
                        elif isinstance(item, int | float) and item < -180:
                            emit(" ")
        elif op in (b"Td", b"TD") and len(operands) >= 2:
            ty = operands[-1]
            if isinstance(ty, int | float) and abs(ty) > 0.5:
                emit("\n")
            elif isinstance(operands[-2], int | float) and operands[-2] > 1:
                emit(" ")
        elif op == b"Tm" and len(operands) >= 6:
            new_y = operands[-1] if isinstance(operands[-1], int | float) else None
            if y is not None and new_y is not None and abs(new_y - y) > 0.5:
                emit("\n")
            elif out and not out[-1].endswith((" ", "\n")):
                emit(" ")
            y = new_y
        elif op == b"T*":
            emit("\n")
        elif op == b"ET":
            if out and not out[-1].endswith("\n"):
                emit("\n")
        elif op == b"Do" and operands and isinstance(operands[-1], Name):
            xo = reader.resolve(xobjects.get(str(operands[-1])))
            if isinstance(xo, Stream):
                sub = xo.dict.get("Subtype")
                if sub == "Image":
                    images += 1
                elif sub == "Form" and depth < 3:
                    text, imgs = page_text(reader, decode_stream(xo, reader.budget),
                                           xo.dict.get("Resources", resources), depth + 1)
                    emit(text)
                    images += imgs
        operands.clear()
    return _tidy("".join(out)), images


def _tidy(text: str) -> str:
    text = text.replace("\r", "\n").replace("\x00", "")
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def read_pdf(path: Path, max_pages: int = 300) -> PdfDoc:
    """The text of each page (up to max_pages). PdfError (plain words) for files that are not usable PDFs."""
    with path.open("rb") as f:
        try:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError as e:  # an empty file
            raise PdfError("That PDF is empty.") from e
        reader: Reader | None = None
        try:
            if b"%PDF-" not in mm[:1024]:
                raise PdfError("That file is not a PDF.")
            reader = Reader(mm)
            if reader.encrypted():
                count = 0
                try:
                    found, count = reader.page_dicts(max_pages)
                    count = count or len(found)
                except PdfError:
                    pass
                return PdfDoc(count, [], encrypted=True, problems=["encrypted"])
            pages_d, declared = reader.page_dicts(max_pages)
            if not pages_d:
                raise PdfError("No pages could be found in that PDF. It may be damaged.")
            doc = PdfDoc(max(declared, len(pages_d)), title=reader.info_title())
            doc.truncated = doc.page_count > len(pages_d)
            for i, pd in enumerate(pages_d, 1):
                try:
                    text, images = page_text(reader, _content(reader, pd), pd.get("Resources"))
                except PdfError as e:
                    if "zip bomb" in str(e):
                        raise
                    text, images = "", 0
                    doc.problems.append(f"page {i}: {e}")
                doc.pages.append(PdfPage(i, text, images))
            return doc
        except RecursionError as e:
            raise PdfError("That PDF is built in a way Delta can't read (too deeply nested).") from e
        finally:
            reader = None
            with contextlib.suppress(BufferError):  # a match object still points into the map; freed when collected
                mm.close()
